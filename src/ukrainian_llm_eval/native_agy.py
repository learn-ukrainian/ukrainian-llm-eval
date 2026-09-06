"""Native AGY subscription adapter with reference-only execution controls.

Only a provisioned OAuth token is copied into a fresh native home. API keys,
user customizations, prior conversations and G1 credit fallback are excluded.
AGY's hook and the parent MCP bridge independently limit reference calls.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
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
_ENV = frozenset({"PATH", "USER", "LOGNAME", "TMPDIR", "SHELL", "TERM", "LANG", "LC_ALL",
                  "SSL_CERT_FILE", "SSL_CERT_DIR"})


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
    if checked["provider"] != PROVIDER:
        raise adapters.AdapterError("AGY requires its native subscription provider")
    checked.update(adapter="agy", agy_bin=binary)
    return checked


def child_env(home: Path) -> dict[str, str]:
    result = {key: value for key, value in os.environ.items() if key in _ENV}
    result.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                  XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"),
                  XDG_STATE_HOME=str(home / ".local/state"))
    return result


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
    if private_env_path is None:
        raise adapters.AdapterError("AGY private authentication provisioning missing")
    root = Path(private_env_path)
    path = root / AUTH_FILE
    try:
        for item in (root, path):
            value = item.lstat()
            if item.is_symlink() or value.st_uid != os.getuid() or value.st_mode & 0o077:
                raise adapters.AdapterError("AGY authentication must be owner-only")
        value = path.stat()
        if not root.is_dir() or not stat.S_ISREG(value.st_mode) or not 0 < value.st_size < MAX_BYTES:
            raise adapters.AdapterError("AGY authentication file invalid")
        return path.read_bytes()
    except OSError as exc:
        raise adapters.AdapterError("AGY authentication unavailable") from exc


def _binary(config: Mapping[str, Any]) -> tuple[str, str]:
    binary = shutil.which(config["agy_bin"])
    if not binary:
        raise adapters.AdapterError("AGY CLI unavailable")
    with tempfile.TemporaryDirectory(prefix="agy-probe-") as temp:
        home = Path(temp)
        result = subprocess.run([binary, "--help"], cwd=home, env=child_env(home),
                                capture_output=True, text=True, timeout=min(15, config["timeout_seconds"]), check=False)
    output = result.stdout + result.stderr
    required = {"--agent", "--model", "--effort", "--json-schema", "--input-format", "--output-format",
                "--disable-slash-commands", "--print-timeout"}
    if result.returncode or len(output) > 100000 or any(flag not in output for flag in required):
        raise adapters.AdapterError("AGY CLI capability unavailable")
    return binary, hashlib.sha256(Path(binary).resolve().read_bytes()).hexdigest()


def preflight(config: Mapping[str, Any], condition: str, sources_url: str | None = None, *,
              private_env_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    checked = validate_config(config)
    adapters._condition_policy(checked, condition, sources_url)
    _credential(private_env_path)
    _, binary_hash = _binary(checked)
    tools, identity = adapters._mcp_list_tools(str(sources_url), checked["timeout_seconds"]) if condition == "sources" else ([], None)
    if condition == "sources" and not set(checked["tools"]) <= {tool["name"] for tool in tools}:
        raise adapters.AdapterError("AGY Sources tool capability unavailable")
    return {"schema": "zno-nmt.capability.v1", "adapter": "agy", "condition": condition,
            "requested_model": checked["model"], "requested_effort": checked["effort"],
            "binary_sha256": binary_hash, "native_controls_sha256": control_hash(checked, condition),
            "tools_sha256": adapters.digest(checked["tools"]), "tool_schema_sha256": adapters.digest(tools),
            "mcp_server_identity_sha256": identity,
            "corpus_id_sha256": adapters.digest(checked["corpus_id"]) if checked["corpus_id"] else None,
            "capability": "native-agy-reference-gated", "max_output_tokens_effective": "unknown"}


class ReferenceServer:
    """Authenticated loopback bridge with serialized calls and private evidence."""

    def __init__(self, config: Mapping[str, Any], condition: str, sources_url: str | None,
                 deadline: float, evidence: Callable[[str, Any], None] | None):
        self.bridge = Bridge(str(sources_url), config["tools"], max_tool_calls=config["max_tool_calls"]) if condition == "sources" else None
        self.token = secrets.token_urlsafe(32)
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()
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
        return f"http://127.0.0.1:{self.server.server_port}/mcp"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def parse_events(stdout: str, packet: Mapping[str, Any], config: Mapping[str, Any],
                 hook_receipts: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    if not hook_receipts or any(receipt.get("decision") != "allow" for receipt in hook_receipts):
        raise adapters.AdapterError("tool_policy_error")
    events = [adapters._strict_json_loads(line) for line in stdout.splitlines() if line.strip()]
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
    responses = adapters._extract_responses(final.get("structured_output"), packet)
    if not hook_receipts or any(receipt.get("decision") != "allow" for receipt in hook_receipts):
        raise adapters.AdapterError("tool_policy_error")
    finish = [receipt for receipt in hook_receipts if receipt.get("call", {}).get("name") == "finish"]
    refs = [receipt for receipt in hook_receipts if receipt.get("call", {}).get("name") == "call_mcp_tool"]
    if len(finish) != 1 or len(hook_receipts) != len(refs) + 1 or hook_receipts[-1] != finish[0]:
        raise adapters.AdapterError("AGY hook evidence incomplete")
    args = finish[0]["call"].get("args", {})
    if {key: value for key, value in args.items() if key not in {"toolSummary", "toolAction"}} != final["structured_output"]:
        raise adapters.AdapterError("AGY native structured evidence mismatch")
    native_refs = [step for step in steps.values() if step.get("step_type") == "tool"]
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
        if not isinstance(content, list) or any(item.get("type") != "text" for item in content):
            raise adapters.AdapterError("AGY reference result type unsupported")
        expected_output = "\n".join(item["text"] for item in content)
        if step["tool_info"].get("output") != expected_output:
            raise adapters.AdapterError("AGY reference result evidence mismatch")
    usage = final.get("usage", {})
    metrics = {name: usage.get(name) for name in ("input_tokens", "output_tokens", "total_tokens")}
    if any(type(value) is not int or value < 0 for value in metrics.values()):
        raise adapters.AdapterError("AGY usage unavailable")
    return {"responses": responses, "session_id": session, "metrics": {**metrics, "cost_usd": None, "tool_calls": len(calls)}}


def run_agy(packet: Mapping[str, Any], config: Mapping[str, Any], condition: str, *, sources_url: str | None,
            prompt: str, private_env_path: str | os.PathLike[str] | None = None,
            evidence: Callable[[str, Any], None] | None = None) -> dict[str, Any]:
    checked = validate_config(config)
    adapters._condition_policy(checked, condition, sources_url)
    credential = _credential(private_env_path)
    binary, binary_hash = _binary(checked)
    started = time.monotonic()
    deadline = started + checked["timeout_seconds"]
    with tempfile.TemporaryDirectory(prefix="agy-eval-") as temp:
        root = Path(temp)
        root.chmod(0o700)
        home, workspace = root / "home", root / "workspace"
        home.mkdir(mode=0o700)
        workspace.mkdir(mode=0o700)
        subprocess.run(["git", "init", "-q", str(workspace)], cwd=root, env=child_env(home),
                       capture_output=True, check=True, timeout=5)
        auth = home / ".gemini/antigravity-cli" / AUTH_FILE
        auth.parent.mkdir(parents=True, mode=0o700)
        auth.write_bytes(credential)
        auth.chmod(0o600)
        (auth.parent / "settings.json").write_text(adapters.canonical(settings(checked, condition)))
        native = home / ".gemini/config"
        (native / "agents").mkdir(parents=True, mode=0o700)
        (native / "agents" / (PROFILE_NAME + ".md")).write_text(profile(condition))
        gate = root / "reference-gate.json"
        gate.write_text(adapters.canonical({"tools": checked["tools"] if condition == "sources" else [],
                                           "max_tool_calls": checked["max_tool_calls"], "deadline": deadline}))
        command = shlex.join([str(adapters._PROJECT_PYTHON), str(Path(__file__).with_name("agy_hook.py")), str(gate)])
        (native / "hooks.json").write_text(adapters.canonical({"evaluator-gate": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": 5}]}]}}))
        schema_path = root / "response-schema.json"
        schema_path.write_text(adapters.canonical(adapters.response_schema(packet)))
        with ReferenceServer(checked, condition, sources_url, deadline, evidence) as reference:
            if condition == "sources":
                (native / "mcp_config.json").write_text(adapters.canonical({"mcpServers": {"sources": {
                    "serverUrl": reference.url, "headers": {"Authorization": "Bearer " + reference.token}}}}))
                tools, _identity = adapters._mcp_list_tools(str(sources_url), checked["timeout_seconds"])
                catalog = [tool for tool in tools if tool["name"] in checked["tools"]]
                prompt += ("\nTrusted reference catalog follows. Use call_mcp_tool with ServerName=sources, "
                           "ToolName and Arguments matching a schema, and required toolSummary/toolAction metadata. "
                           "No filesystem schema lookup is needed.\n" + adapters.canonical(catalog))
            argv = [binary, "--input-format", "stream-json", "--output-format", "stream-json",
                    "--agent", PROFILE_NAME, "--model", checked["model"], "--effort", checked["effort"],
                    "--json-schema", str(schema_path), "--disable-slash-commands",
                    "--print-timeout", str(checked["timeout_seconds"]) + "s"]
            env = child_env(home)
            if evidence is not None:
                evidence("cli_invocation", {"argv": argv, "env_keys": sorted(env), "binary_sha256": binary_hash,
                                             "native_controls_sha256": control_hash(checked, condition)})
            result = adapters._run_claude_process(argv, cwd=workspace, env=env,
                prompt=adapters.canonical({"event": "user", "message": {"content": prompt}}) + "\n",
                timeout=max(1, int(deadline - time.monotonic())), evidence=evidence)
            if evidence is not None:
                evidence("cli_result", {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
            receipts = [adapters._strict_json_loads(line) for line in gate.with_suffix(".jsonl").read_text().splitlines()] if gate.with_suffix(".jsonl").exists() else []
            if evidence is not None:
                evidence("agy_hook_receipts", receipts)
            if result.returncode or reference.error is not None or len(result.stdout.encode()) > MAX_BYTES:
                raise adapters.AdapterError("AGY native execution failed")
            if hashlib.sha256(Path(binary).resolve().read_bytes()).hexdigest() != binary_hash:
                raise adapters.AdapterError("AGY binary changed during execution")
            parsed = parse_events(result.stdout, packet, checked, receipts, reference.calls)
            return {"responses": parsed["responses"], "metrics": {
                **parsed["metrics"], "elapsed_seconds": time.monotonic() - started}, "identity": {
                    "adapter": "agy", "harness": "agy-cli", "provider": PROVIDER, "model": checked["model"],
                    "session_id": parsed["session_id"], "requested_model": checked["model"],
                    "effective_model": checked["model"], "requested_effort": checked["effort"],
                    "effective_effort": "unknown", "binary_sha256": binary_hash,
                    "native_controls_sha256": control_hash(checked, condition),
                    "max_output_tokens_configured": checked["max_output_tokens"],
                    "max_output_tokens_effective": "unknown", "g1_credit_fallback": False,
                    "auxiliary_title_generation": "native-harness-metadata; not candidate output",
                    "tool_schema_sha256": adapters.digest(checked["tools"] if condition == "sources" else []),
                    "corpus_id_sha256": adapters.digest(checked["corpus_id"]) if checked["corpus_id"] else None,
                }}
