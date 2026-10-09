"""Native AGY subscription adapter with reference-only execution controls.

Only a provisioned OAuth token is copied into a fresh native home. API keys,
user customizations, prior conversations and G1 credit fallback are excluded.
AGY's hook and the parent MCP bridge independently limit reference calls.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self

from . import adapters
from .mcp_proxy import Bridge

PROVIDER = "managed:antigravity-subscription"
AUTH_FILE = "antigravity-oauth-token"
PROFILE_NAME = "ukrainian-evaluation"
MAX_BYTES = 2_000_000


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config, Mapping) or config.get("adapter") != "agy":
        raise adapters.AdapterError("wrong native AGY adapter")
    checked = dict(config)
    binary = checked.pop("agy_bin", "agy")
    if not isinstance(binary, str) or not binary.strip() or "claude_bin" in checked:
        raise adapters.AdapterError("AGY binary configuration invalid")
    checked["adapter"] = "claude"
    checked = adapters.validate_config(checked)
    effort = checked["effort"]
    if effort not in {"low", "medium", "high"} or checked["model"] != "gemini-3.8-flash-" + effort:
        raise adapters.AdapterError("AGY model and effort configuration conflict")
    if checked.get("provider") != PROVIDER:
        raise adapters.AdapterError("AGY requires its native subscription provider")
    checked.update(adapter="agy", agy_bin=binary)
    return checked


def child_env(home: Path) -> dict[str, str]:
    attempt = adapters._ATTEMPT.get()
    return attempt["env"] if attempt is not None else adapters._isolated_environment(home, home)


def profile(condition: str) -> str:
    return ("---\nname: " + PROFILE_NAME + "\ndescription: Ukrainian evaluation.\n"
            "tools: [finish]\nmainAgent: true\nsubagent: false\nmodel: inherit\n"
            "inheritCustomizations: true\ninheritMcp: " + str(condition == "sources").lower() + "\n"
            'commandExecutionPolicy: "off"\nmcpServers: []\nskills: []\nplugins: []\n---\n\n'
            "# System Prompt\nAnswer the supplied Ukrainian evaluation packet and obey its response contract.\n")


def settings(config: Mapping[str, Any], condition: str) -> dict[str, Any]:
    return {"useG1Credits": False, "permissions": {
        "allow": ["mcp(sources/" + name + ")" for name in config["tools"]] if condition == "sources" else [],
        "deny": ["command(*)", "read_file(*)", "write_file(*)", "read_url(*)"],
    }}


def control_hash(config: Mapping[str, Any], condition: str) -> str:
    return adapters.digest({"profile": profile(condition), "settings": settings(config, condition),
                            "hook_sha256": hashlib.sha256(Path(__file__).with_name("agy_hook.py").read_bytes()).hexdigest(),
                            "tools": config["tools"] if condition == "sources" else [],
                            "max_tool_calls": config["max_tool_calls"], "input": "one-user-event-stdin"})


def _credential(private_env_path: str | os.PathLike[str] | None) -> bytes:
    try:
        if private_env_path is None:
            raise adapters.AdapterError("missing")
        return adapters._checked_private_read(Path(private_env_path), AUTH_FILE, limit=MAX_BYTES)
    except (adapters.AdapterError, OSError, TypeError):
        raise adapters.AuthUnavailable("AGY authentication must be owner-only and safely staged") from None


@adapters._isolated_native
def _binary(config: Mapping[str, Any]) -> tuple[str, str]:
    binary = shutil.which(config["agy_bin"])
    if not binary:
        raise adapters.AdapterError("AGY CLI unavailable")
    attempt = adapters._ATTEMPT.get()
    result = subprocess.run([binary, "--help"], cwd=attempt["cwd"], env=child_env(attempt["home"]),
                            capture_output=True, text=True, timeout=adapters._remaining_native_deadline(min(15, config["timeout_seconds"])),
                            umask=0o077, check=False)
    output = result.stdout + result.stderr
    required = {"--agent", "--model", "--effort", "--json-schema", "--input-format", "--output-format",
                "--disable-slash-commands", "--print-timeout", "--log-file"}
    if result.returncode or len(output) > 100000 or any(flag not in output for flag in required):
        raise adapters.AdapterError("AGY CLI capability unavailable")
    return binary, hashlib.sha256(Path(binary).resolve().read_bytes()).hexdigest()


@adapters._isolated_native
def preflight(config: Mapping[str, Any], condition: str, sources_url: str | None = None, *,
              private_env_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    checked = validate_config(config)
    adapters._condition_policy(checked, condition, sources_url)
    _credential(private_env_path)
    _, binary_hash = _binary(checked)
    tools, identity = adapters._mcp_list_tools(str(sources_url), checked["timeout_seconds"]) if condition == "sources" else ([], None)
    if condition == "sources" and not set(checked["tools"]) <= {tool["name"] for tool in tools}:
        raise adapters.AdapterError("AGY Sources tool capability unavailable")
    tools = adapters._reference_catalog(tools, checked["tools"]) if condition == "sources" else []
    return {"reference_catalog": tools, "schema": "zno-nmt.capability.v1", "adapter": "agy", "condition": condition,
            "requested_model": checked["model"], "requested_effort": checked["effort"],
            "binary_sha256": binary_hash, "native_controls_sha256": control_hash(checked, condition),
            "tools_sha256": adapters.digest(checked["tools"]), "tool_schema_sha256": adapters.digest(tools),
            "mcp_server_identity_sha256": identity,
            "corpus_id_sha256": adapters.digest(checked["corpus_id"]) if checked["corpus_id"] else None,
            "capability": "native-agy-reference-gated", **adapters.native_output_limit_metadata(checked), "capture_stdout_max_bytes": MAX_BYTES}


INPUT_FRAME_PREFIX = '{"event":"user","message":{"content":'
INPUT_FRAME_SUFFIX = "}}\n"

ARTIFACT_LIMIT = 16 * 1024 * 1024
_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_CONVERSATION = re.compile(r"\b(?:Created|found) conversation\s+(" + _UUID + r")\b")
_POINTER = re.compile(r"file://[^\s\"'<>]+")


def capture_native_artifacts(root: Path, log: Path, app_data: Path, evidence) -> str:
    """Retain only the unique log-bound conversation; diagnostics cannot attest tools.

    No transcript field has yet been proved to bind complete results to exact
    native call IDs. The retained bytes are deliberately never parser backfill.
    """
    def retain(path: Path, label: str) -> bytes:
        try:
            relative = path.relative_to(root).as_posix()
            raw = adapters._checked_private_read(root, relative, limit=ARTIFACT_LIMIT,
                                                 mode=None, allow_empty=False)
        except (ValueError, adapters.AdapterError):
            raise adapters.AdapterError("AGY native artifact unavailable or unsafe") from None
        if evidence is not None:
            evidence("agy_native_artifact", {"kind": label, "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw), "raw_base64": base64.b64encode(raw).decode("ascii")})
        return raw

    try:
        raw_log = retain(log, "log")
        # CLI 1.3.2 repeats the bound identity on several log lines; only distinct identities are ambiguous.
        identities = set(_CONVERSATION.findall(raw_log.decode("utf-8", errors="strict")))
        if len(identities) != 1:
            raise adapters.AdapterError("AGY native conversation binding missing or ambiguous")
        session = identities.pop()
        conversation = app_data / "brain" / session
        transcript = conversation / ".system_generated/logs/transcript.jsonl"
        raw = retain(transcript, "transcript")
        text = raw.decode("utf-8", errors="strict")
        if not raw.endswith(b"\n"):
            raise adapters.AdapterError("AGY native transcript incomplete")
        rows = [adapters._strict_json_loads(line) for line in adapters._physical_lines(text)]
        if not rows or any(not isinstance(row, dict) for row in rows):
            raise adapters.AdapterError("AGY native transcript malformed")
        # Only literal producer pointers under this conversation's steps roots.
        # They are retained verbatim, never stripped, inlined or paired by order.
        result_texts = [row.get("content", "") for row in rows if row.get("type") in {"GENERIC", "MCP_TOOL"}
                        and isinstance(row.get("content"), str)]
        for uri in {uri for content in result_texts for uri in _POINTER.findall(content)}:
            parsed = urllib.parse.urlsplit(uri)
            if parsed.netloc or parsed.query or parsed.fragment:
                raise adapters.AdapterError("AGY native result pointer unsafe")
            path = Path(urllib.parse.unquote(parsed.path))
            relative = path.relative_to(conversation)
            if not (relative.parts[:1] == ("steps",) or relative.parts[:2] == (".system_generated", "steps")):
                raise adapters.AdapterError("AGY native result pointer unsafe")
            retain(path, "tool-result")
        if evidence is not None:
            evidence("agy_native_capture", {"conversation_id": session, "diagnostic_only": True,
                                            "complete_tool_provenance": "unknown"})
        return session
    except (UnicodeError, ValueError, OSError) as exc:
        if evidence is not None:
            evidence("agy_native_capture_failure", {"reason": "native_artifact_unavailable_or_unsafe"})
        if isinstance(exc, adapters.AdapterError):
            raise
        raise adapters.AdapterError("AGY native artifact unavailable or unsafe") from None


class ReferenceServer:
    """Authenticated loopback bridge with serialized calls and private evidence."""

    def __init__(self, config: Mapping[str, Any], condition: str, sources_url: str | None,
                 deadline: float, evidence: Callable[[str, Any], None] | None):
        self.bridge = Bridge(str(sources_url), config["tools"], max_tool_calls=config["max_tool_calls"]) if condition == "sources" else None
        self.token = secrets.token_urlsafe(32)
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []
        self.metadata: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server = None
        self.thread = None
        if self.bridge is None:
            return
        self.bridge.deadline = deadline
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(max(0.1, min(15, deadline - time.monotonic())))

            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                if self.headers.get("Authorization") != "Bearer " + owner.token:
                    self.send_error(403)
                    return
                try:
                    with owner.lock:
                        size = int(self.headers.get("Content-Length", "0"))
                        if (owner.bridge is None or owner.error is not None or self.path != "/mcp"
                                or not 0 < size <= MAX_BYTES or time.monotonic() >= deadline):
                            raise adapters.AdapterError("AGY reference request rejected")
                        body = adapters._strict_json_loads(self.rfile.read(size).decode())
                        owner.bridge.timeout = max(0.1, min(20, deadline - time.monotonic()))
                        if evidence is not None:
                            evidence("agy_mcp_request", body)
                        reply = owner.bridge.handle(body)
                        if body.get("method") != "tools/call":
                            owner.metadata.append({"request": body, "response": reply})
                        if evidence is not None:
                            evidence("agy_mcp_response", reply)
                        if body.get("method") == "tools/call":
                            if reply is None or "error" in reply or reply.get("result", {}).get("isError"):
                                raise adapters.AdapterError("AGY reference failed")
                            owner.calls.append({"name": body["params"]["name"], "arguments": body["params"].get("arguments", {}),
                                                "result": reply["result"]})
                        data = adapters.canonical(reply).encode() if reply is not None else b""
                        self.send_response(200 if data else 204)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                except Exception as exc:  # noqa: BLE001 -- retain safe failure classification
                    owner.error = exc
                    self.send_error(400, "Evaluator reference request rejected")

            def do_GET(self) -> None:
                self.send_error(405)

            def do_DELETE(self) -> None:
                self.send_error(405)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        if self.server is None:
            raise adapters.AdapterError("closed-book has no reference proxy")
        return f"http://127.0.0.1:{self.server.server_port}/mcp"

    def __enter__(self) -> Self:
        if self.thread is not None:
            self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def _finish_completes(prior: Mapping[str, Any], step: Mapping[str, Any]) -> bool:
    # CLI 1.3.2 reports finish twice at one step_index: ACTIVE as a finish tool call, then DONE as a finish step.
    prior_info = prior.get("tool_info", {})
    step_info = step.get("tool_info", {})
    return (prior.get("state") == "ACTIVE" and prior.get("step_type") == "tool" and prior.get("tool_name") == "finish"
            and prior_info.get("name", "finish") == "finish"
            and step.get("state") == "DONE" and step.get("step_type") == "finish"
            and step.get("conversation_id") == prior.get("conversation_id")
            and step.get("tool_name", "finish") == "finish"
            and step_info.get("name", "finish") == "finish"
            and step_info.get("parameters", prior_info.get("parameters")) == prior_info.get("parameters"))


def parse_events(stdout: str, packet: Mapping[str, Any], config: Mapping[str, Any],
                 hook_receipts: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    if not hook_receipts or any(receipt.get("decision") != "allow" for receipt in hook_receipts):
        raise adapters.AdapterError("tool_policy_error")
    events = [adapters._strict_json_loads(line) for line in adapters._physical_lines(stdout) if line.strip()]
    if not events or any(not isinstance(event, dict) for event in events):
        raise adapters.AdapterError("AGY native response missing")
    if events[0].get("event") != "init" or events[-1].get("event") != "result":
        raise adapters.AdapterError("AGY native response envelope invalid")
    init = events[0]["init"]
    if init.get("model") != config["model"] or init.get("agent") != PROFILE_NAME:
        raise adapters.AdapterError("AGY native model or agent drift")
    schema = adapters.response_schema(packet)
    if init.get("json_schema") != schema:
        raise adapters.AdapterError("AGY native schema drift")
    steps: dict[int, dict[str, Any]] = {}
    sessions: set[str] = set()
    for event in events[1:-1]:
        if event.get("event") != "step_update":
            raise adapters.AdapterError("AGY unexpected native event")
        step = event["step_update"]
        if step.get("step_type") not in {"user_input", "agent_response", "tool", "finish"}:
            raise adapters.AdapterError("AGY unexpected native step type")
        if step.get("subagent_info") or step.get("error") or step.get("tool_info", {}).get("error"):
            raise adapters.AdapterError("AGY tool execution failed")
        if step.get("state") not in {"ACTIVE", "DONE"} or type(step.get("step_index")) is not int:
            raise adapters.AdapterError("AGY native step invalid")
        sessions.add(step.get("conversation_id"))
        prior = steps.get(step["step_index"])
        if prior is not None and _finish_completes(prior, step):
            steps[step["step_index"]] = {**prior, **step, "step_type": "finish"}
            continue
        if prior is not None and (prior.get("state") == "DONE" or step.get("state") != "DONE"
                or any(prior.get(key) != step.get(key) for key in ("conversation_id", "step_type", "tool_name"))
                or prior.get("tool_info", {}).get("parameters") != step.get("tool_info", {}).get("parameters")
                or ("output" in prior.get("tool_info", {})
                    and prior["tool_info"]["output"] != step.get("tool_info", {}).get("output"))):
            raise adapters.AdapterError("AGY duplicate or conflicting native step")
        steps[step["step_index"]] = step
    if any(step.get("state") != "DONE" for step in steps.values()):
        raise adapters.AdapterError("AGY unfinished native step")
    if sum(step.get("step_type") == "user_input" for step in steps.values()) != 1 or sum(
        step.get("step_type") == "finish" for step in steps.values()
    ) != 1:
        raise adapters.AdapterError("AGY turn boundary invalid")
    final = events[-1]["result"]
    session = final.get("conversation_id")
    if (not isinstance(session, str) or not session or sessions != {session}
            or final.get("status") != "SUCCESS" or final.get("error") or final.get("num_turns") != 1
            or final.get("json_schema") != schema):
        raise adapters.AdapterError("AGY final response invalid")
    parsed = adapters._extract_enveloped_responses(final.get("structured_output"), packet)
    responses = parsed[0]
    finish = [receipt for receipt in hook_receipts if receipt.get("call", {}).get("name") == "finish"]
    refs = [receipt for receipt in hook_receipts if receipt.get("call", {}).get("name") == "call_mcp_tool"]
    metadata = [receipt for receipt in hook_receipts if receipt.get("call", {}).get("name") == "list_resources"]
    if (len(metadata) > adapters.METADATA_LIMIT or any(not config["tools"] or receipt["call"].get("args") != {"ServerName": "sources"}
                                                      for receipt in metadata)):
        raise adapters.AdapterError("tool_policy_error")
    if len(finish) != 1 or len(hook_receipts) != len(refs) + len(metadata) + 1 or hook_receipts[-1] != finish[0]:
        raise adapters.AdapterError("AGY hook evidence incomplete")
    args = finish[0]["call"].get("args", {})
    finish_payload = {key: value for key, value in args.items() if key not in {"toolSummary", "toolAction"}}
    if finish_payload != final["structured_output"]:
        raise adapters.AdapterError("AGY native structured evidence mismatch")
    if adapters._extract_enveloped_responses(finish_payload, packet) != parsed:
        raise adapters.AdapterError("AGY native structured evidence mismatch")
    native_meta = [step for step in steps.values() if step.get("step_type") == "tool" and step.get("tool_name") == "list_resources"]
    if len(native_meta) != len(metadata) or any(step.get("tool_info", {}).get("parameters") != {"ServerName": "sources"}
                                               or "output" not in step.get("tool_info", {}) for step in native_meta):
        raise adapters.AdapterError("AGY native metadata evidence missing or changed")
    native_refs = [step for step in steps.values() if step.get("step_type") == "tool" and step not in native_meta]
    if len(native_refs) != len(calls) or len(refs) != len(calls) or len(calls) > config["max_tool_calls"]:
        raise adapters.AdapterError("AGY reference count mismatch")
    for hook, step, call in zip(refs, native_refs, calls, strict=True):
        args = hook["call"].get("args", {})
        params = step.get("tool_info", {}).get("parameters", {})
        if (step.get("tool_name") != "call_mcp_tool" or args.get("ServerName") != "sources"
                or args.get("ToolName") != call["name"] or args.get("Arguments") != call["arguments"]
                or params != {"ServerName": "sources", "ToolName": call["name"], "Arguments": call["arguments"]}):
            raise adapters.AdapterError("AGY reference evidence mismatch")
        content = call["result"].get("content", [])
        if call["result"].get("isError") or not isinstance(content, list) or any(item.get("type") != "text" for item in content):
            raise adapters.AdapterError("AGY reference result type unsupported")
        expected_output = "\n".join(item["text"] for item in content)
        if step["tool_info"].get("output") != expected_output:
            raise adapters.AdapterError("AGY reference result evidence mismatch")
    signatures = [adapters.digest({"name": call["name"], "arguments": call["arguments"]}) for call in calls]
    if len(set(signatures)) != len(signatures):
        raise adapters.AdapterError("AGY repeated call lacks unique producer correlation")
    usage = final.get("usage", {})
    metrics = {name: usage.get(name) for name in ("input_tokens", "output_tokens", "total_tokens")}
    if any(type(value) is not int or value < 0 for value in metrics.values()):
        raise adapters.AdapterError("AGY usage unavailable")
    return {"responses": responses, "session_id": session, "metrics": {**metrics, "cost_usd": None,
            "tool_calls": len(calls), "native_metadata_operations": len(metadata)}}


@adapters._isolated_native
def run_agy(packet: Mapping[str, Any], config: Mapping[str, Any], condition: str, *, sources_url: str | None,
            prompt: str, private_env_path: str | os.PathLike[str] | None = None,
            evidence: Callable[[str, Any], None] | None = None,
            reference_catalog: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    checked = validate_config(config)
    adapters._condition_policy(checked, condition, sources_url)
    credential = _credential(private_env_path)
    binary, binary_hash = _binary(checked)
    if evidence is not None:
        evidence("runtime_scaffolding", {"prefix": INPUT_FRAME_PREFIX, "suffix": INPUT_FRAME_SUFFIX,
            "sha256": hashlib.sha256((INPUT_FRAME_PREFIX + INPUT_FRAME_SUFFIX).encode()).hexdigest(),
            "condition": condition})
    started = time.monotonic()
    deadline = adapters._ATTEMPT.get()["deadline"]
    with contextlib.nullcontext(adapters._ATTEMPT.get()) as attempt:
        root, home, workspace = attempt["root"], attempt["home"], attempt["cwd"]
        subprocess.run(["git", "init", "-q", str(workspace)], cwd=root, env=child_env(home),
                       capture_output=True, check=True, timeout=adapters._remaining_native_deadline(5), umask=0o077)
        (home / ".gemini").mkdir(mode=0o700)
        auth = home / ".gemini/antigravity-cli" / AUTH_FILE
        auth.parent.mkdir(parents=True, mode=0o700)
        adapters._exclusive_private_write(auth, credential)
        (auth.parent / "settings.json").write_text(adapters.canonical(settings(checked, condition)))
        native = home / ".gemini/config"
        (native / "agents").mkdir(parents=True, mode=0o700)
        (native / "agents" / (PROFILE_NAME + ".md")).write_text(profile(condition))
        gate = root / "reference-gate.json"
        gate.write_text(adapters.canonical({"tools": checked["tools"] if condition == "sources" else [],
                                           "max_tool_calls": checked["max_tool_calls"],
                                           "max_metadata_operations": adapters.METADATA_LIMIT, "deadline": deadline}))
        command = shlex.join([str(adapters._PROJECT_PYTHON), str(Path(__file__).with_name("agy_hook.py")), str(gate)])
        (native / "hooks.json").write_text(adapters.canonical({"evaluator-gate": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": 5}]}]}}))
        schema_path = root / "response-schema.json"
        schema_path.write_text(adapters.canonical(adapters.response_schema(packet)))
        with ReferenceServer(checked, condition, sources_url, deadline, evidence) as reference:
            if condition == "sources":
                (native / "mcp_config.json").write_text(adapters.canonical({"mcpServers": {"sources": {
                    "serverUrl": reference.url, "headers": {"Authorization": "Bearer " + reference.token}}}}))
            argv = [binary, "--input-format", "stream-json", "--output-format", "stream-json",
                    "--agent", PROFILE_NAME, "--model", checked["model"], "--effort", checked["effort"],
                    "--json-schema", str(schema_path), "--disable-slash-commands",
                    "--print-timeout", str(checked["timeout_seconds"]) + "s"]
            log = root / "native-log.txt"
            adapters._exclusive_private_write(log, b"")
            argv.extend(["--log-file", str(log)])
            env = child_env(home)
            if evidence is not None:
                evidence("cli_invocation", {"argv": argv, "env_keys": sorted(env), "binary_sha256": binary_hash,
                                             "native_controls_sha256": control_hash(checked, condition)})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise adapters.AdapterError("AGY timeout before candidate execution")
            execution_error = None
            try:
                result = adapters._run_claude_process(argv, cwd=workspace, env=env,
                    prompt=INPUT_FRAME_PREFIX + json.dumps(prompt, ensure_ascii=False, allow_nan=False) + INPUT_FRAME_SUFFIX,
                    timeout=remaining, evidence=evidence)
                if not adapters._has_model_output(result.stdout) and adapters._auth_failure({"stdout": result.stdout, "stderr": result.stderr}):
                    raise adapters.AuthUnavailable("auth_unavailable")
            except BaseException as exc:
                execution_error = exc
                raise
            finally:
                if isinstance(execution_error, adapters.AuthUnavailable):
                    if evidence is not None:
                        evidence("agy_native_capture_failure", {"reason": "auth_unavailable"})
                else:
                    try:
                        captured_session = capture_native_artifacts(root, log, home / ".gemini/antigravity-cli", evidence)
                    except adapters.AdapterError:
                        if execution_error is None:
                            raise
            if evidence is not None:
                evidence("cli_result", {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
            receipt_text = gate.with_suffix(".jsonl").read_text() if gate.with_suffix(".jsonl").exists() else ""
            if evidence is not None:
                evidence("agy_hook_receipts_raw", {"text": receipt_text})
            receipts = [adapters._strict_json_loads(line) for line in adapters._physical_lines(receipt_text)]
            if evidence is not None:
                evidence("agy_hook_receipts", receipts)
            if result.returncode or reference.error is not None or len(result.stdout.encode()) > MAX_BYTES:
                raise adapters.AdapterError("AGY native execution failed")
            if hashlib.sha256(Path(binary).resolve().read_bytes()).hexdigest() != binary_hash:
                raise adapters.AdapterError("AGY binary changed during execution")
            parsed = parse_events(result.stdout, packet, checked, receipts, reference.calls)
            if parsed["session_id"] != captured_session:
                raise adapters.AdapterError("AGY log/native conversation identity conflict")
            parsed["metrics"]["controller_metadata_operations"] = len(reference.metadata)
            return {"responses": parsed["responses"], "metrics": {
                **parsed["metrics"], "elapsed_seconds": time.monotonic() - started}, "identity": {
                    "adapter": "agy", "harness": "agy-cli", "provider": PROVIDER, "model": checked["model"],
                    "session_id": parsed["session_id"], "requested_model": checked["model"],
                    "effective_model": checked["model"], "requested_effort": checked["effort"],
                    "effective_effort": "unknown", "binary_sha256": binary_hash,
                    "native_controls_sha256": control_hash(checked, condition),
                    **adapters.native_output_limit_metadata(checked),
                    "capture_stdout_max_bytes": MAX_BYTES, "g1_credit_fallback": False,
                    "auxiliary_title_generation": "native-harness-metadata; not candidate output",
                    "tool_schema_sha256": adapters.digest(checked["tools"] if condition == "sources" else []),
                    "corpus_id_sha256": adapters.digest(checked["corpus_id"]) if checked["corpus_id"] else None,
                }}
