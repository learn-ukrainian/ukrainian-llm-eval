"""Explicit reference-only native Codex policy, shared by both conditions."""

from __future__ import annotations

import hashlib
import os
import sysconfig
import tempfile
import time
from pathlib import Path
from typing import Any

from . import adapters, codex_catalog
from . import native_codex as native
from .candidate_outcome import CANDIDATE_RESPONSE_ERROR
from .codex_reference_bridge import snapshot

CONTROL_FILE = "reference-control.json"
CONTROL_SCHEMA = "native-codex-reference-control.v1"
CASES = ("closed-book", "reference-call", "resource-list", "resource-templates", "resource-read", "call-cap",
         "denied-tool", "schema-drift", "missing-tool")


def implementation_hash() -> str:
    files = ("codex_catalog.py", "codex_reference.py", "codex_reference_bridge.py",
             "codex_reference_controls.py", "native_codex.py", "codex_controls.py", "mcp_proxy.py",
             "adapters.py", "candidate_outcome.py")
    return adapters.digest({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                            for name in files})


def bridge_command() -> Path:
    path = Path(sysconfig.get_path("scripts")) / "ukrainian-llm-eval-codex-reference"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise native._fail("native reference controller is not installed in this environment")
    return path.resolve()


def request_shape(config: dict, condition: str, catalog_identity: dict) -> dict:
    return {
        "policy": "reference-only", "condition": condition, "model": config["model"],
        "effort": config["effort"], "catalog": catalog_identity,
        "closed_book_controls": native._request_shape(config),
        "reference_tools": config["tools"] if condition == "sources" else [],
        "max_tool_calls": config["max_tool_calls"], "resources": "controller-denied",
        "catalog_override": "exact bundled model with explicit tool-policy fields only",
        "user_input": False, "required_mcp": condition == "sources",
    }


def condition_policy(config: dict, condition: str, sources_url: str | None) -> None:
    adapters._condition_policy(config, condition, sources_url)
    if condition == "closed-book" and sources_url is not None:
        raise native._fail("closed-book does not accept a Sources URL")


def load_control(root: Path, config: dict, probe: native._CliProbe, catalog_identity: dict) -> dict:
    path = native._private_file(root, CONTROL_FILE, "native reference control receipt")
    receipt = adapters._strict_json_loads(path.read_text())
    expected = {
        "schema": CONTROL_SCHEMA, "status": "passed", "model": config["model"], "effort": config["effort"],
        "tools": config["tools"], "max_tool_calls": config["max_tool_calls"],
        "entrypoint_sha256": probe.entrypoint_sha256, "native_runtime_sha256": probe.native_runtime_sha256,
        "cli_version": probe.version, "implementation_sha256": implementation_hash(),
        "catalog_identity": catalog_identity,
        "bridge_entrypoint_sha256": hashlib.sha256(bridge_command().read_bytes()).hexdigest(),
    }
    if not isinstance(receipt, dict) or set(receipt) != set(expected) | {"schemas", "source_server_sha256", "captures"}:
        raise native._fail("native reference control receipt schema is invalid")
    if any(receipt[key] != value for key, value in expected.items()):
        raise native._fail("native reference control receipt identity or policy drift")
    schemas = receipt["schemas"]
    from .codex_reference_bridge import normalized_tools

    if normalized_tools(schemas, config["tools"]) != schemas:
        raise native._fail("native reference control schemas are invalid")
    if not isinstance(receipt["source_server_sha256"], str) or native.re.fullmatch(
        r"[0-9a-f]{64}", receipt["source_server_sha256"]
    ) is None:
        raise native._fail("native reference server identity is invalid")
    captures = receipt["captures"]
    if not isinstance(captures, dict) or set(captures) != set(CASES):
        raise native._fail("native reference control coverage is incomplete")
    for case, entry in captures.items():
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise native._fail("native reference capture locator is invalid")
        artifact = Path(entry["path"])
        if (not artifact.is_absolute() or artifact.is_symlink() or not artifact.is_file()
                or artifact.stat().st_size > native._MAX_CONTROL_ARTIFACT_BYTES):
            raise native._fail("native reference capture unavailable")
        raw = artifact.read_bytes()
        capture = adapters._strict_json_loads(raw.decode())
        if (hashlib.sha256(raw).hexdigest() != entry["sha256"] or not isinstance(capture, dict)
                or capture.get("case") != case or capture.get("passed") is not True):
            raise native._fail("native reference capture drift or failed control")
    return receipt


def prepare(config: dict, condition: str, sources_url: str | None, private_env_path: Any):
    config = native.validate_config(config)
    condition_policy(config, condition, sources_url)
    root = native._owner_directory(private_env_path, control_file=CONTROL_FILE)
    native._sanitized_chatgpt_auth(root)
    probe = native._probe_cli(config["codex_bin"], config["timeout_seconds"])
    catalog, catalog_identity = codex_catalog.bundled_catalog(probe, config["model"], config["timeout_seconds"])
    receipt = load_control(root, config, probe, catalog_identity)
    if condition == "sources":
        try:
            schemas, server = snapshot(sources_url, config["tools"], config["timeout_seconds"], config["max_tool_calls"])
        except Exception:  # noqa: BLE001 - upstream diagnostics may contain private endpoints
            raise native._fail("native reference metadata unavailable before inference") from None
        if schemas != receipt["schemas"] or server != receipt["source_server_sha256"]:
            raise native._fail("native reference schema or server drift before inference")
    return config, root, probe, catalog, catalog_identity, receipt


def identity(config: dict, condition: str, probe: native._CliProbe, catalog_identity: dict, receipt: dict) -> dict:
    sources = condition == "sources"
    return {
        "adapter": "codex", "harness": native.CODEX_HARNESS, "provider": native.CODEX_PROVIDER,
        "model": config["model"], "requested_model": config["model"], "requested_model_alias": config["model"],
        "effective_model": "unknown", "effective_backend_model": "unknown", "account_identity": "unknown",
        "requested_effort": config["effort"], "accepted_effort": "unknown", "effective_effort": "unknown",
        "cli_version": probe.version, "version_observed": probe.version,
        "entrypoint_sha256": probe.entrypoint_sha256, "native_runtime_sha256": probe.native_runtime_sha256,
        "control_receipt_sha256": adapters.digest(receipt), "settings_sha256": native._settings_hash(config),
        "request_shape_sha256": adapters.digest(request_shape(config, condition, catalog_identity)),
        "tool_schema_sha256": adapters.digest(receipt["schemas"] if sources else []),
        "tools_sha256": adapters.digest(config["tools"] if sources else []),
        "mcp_server_identity_sha256": receipt["source_server_sha256"] if sources else None,
        "corpus_id_sha256": adapters.digest(config["corpus_id"]) if sources and config["corpus_id"] else None,
        "max_output_tokens_configured": config["max_output_tokens"], "max_output_tokens_effective": "unknown",
        "codex_tool_policy": "reference-only", **catalog_identity,
    }


def preflight(config, condition, sources_url=None, *, private_env_path=None):
    config, _root, probe, _catalog, catalog_identity, receipt = prepare(
        config, condition, sources_url, private_env_path,
    )
    return {**identity(config, condition, probe, catalog_identity, receipt),
            "schema": native.CODEX_CAPABILITY_SCHEMA, "condition": condition,
            "capability": "native-codex-reference-controls"}


def reference_overrides(config_path: Path, tools: list[str]) -> tuple[str, ...]:
    argv = ["-c", "mcp_servers.sources.command=" + adapters.canonical(str(bridge_command())),
            "-c", "mcp_servers.sources.args=" + adapters.canonical(["--config", str(config_path)]),
            "-c", "mcp_servers.sources.enabled_tools=" + adapters.canonical(tools),
            "-c", "mcp_servers.sources.required=true"]
    for tool in tools:
        argv += ["-c", f'mcp_servers.sources.tools.{tool}.approval_mode="approve"']
    return tuple(argv)


def parse_events(stdout: str, packet: dict, tools: list[str], journal: list[dict]):
    if any(not isinstance(entry, dict) for entry in journal):
        raise native._fail("native reference controller evidence is invalid")
    pending, completed, filtered = {}, [], []
    seen = set()
    in_turn = False
    for line in stdout.splitlines():
        event = adapters._strict_json_loads(line)
        if not isinstance(event, dict):
            raise native._fail("native reference event is invalid")
        if event.get("type") == "turn.started":
            in_turn = True
        if event.get("type") == "turn.completed":
            in_turn = False
        item = event.get("item", {})
        if isinstance(item, dict) and item.get("type") == "mcp_tool_call":
            if (not in_turn or item.get("server") != "sources" or item.get("tool") not in tools
                    or not isinstance(item.get("arguments"), dict) or not isinstance(item.get("id"), str)):
                raise native._fail("native reference tool event violates policy")
            key = item["id"]
            signature = (item["tool"], adapters.digest(item["arguments"]))
            if event["type"] == "item.started" and key not in seen and item.get("status") == "in_progress":
                seen.add(key)
                pending[key] = signature
            elif (event["type"] == "item.completed" and item.get("status") in {"completed", "failed"}
                  and pending.pop(key, None) == signature):
                completed.append(signature)
            else:
                raise native._fail("native reference call lifecycle is invalid")
        else:
            filtered.append(line)
    if pending:
        raise native._fail("native reference call was interrupted")
    call_indices = [entry.get("index") for entry in journal if entry.get("event") == "call"]
    result_indices = [entry.get("index") for entry in journal if entry.get("event") == "result"]
    if (call_indices != list(range(1, len(call_indices) + 1)) or result_indices != call_indices
            or any(not isinstance(entry, dict) or entry.get("event") not in {"ready", "call", "result", "rejected"}
                   for entry in journal)):
        raise native._fail("native reference controller evidence is incomplete")
    recorded = [(entry.get("tool"), entry.get("arguments_sha256")) for entry in journal
                if entry.get("event") == "call" or entry.get("reason") == "call_cap"]
    if recorded != completed:
        raise native._fail("native reference controller and CLI evidence disagree")
    return native._parse_events("\n".join(filtered), packet), len(completed)


def run(packet, config, condition, *, sources_url, prompt, evidence=None, private_env_path=None):
    if not isinstance(prompt, str) or not prompt:
        raise native._fail("Codex prompt must be a nonempty string")
    config, root, probe, catalog, catalog_identity, receipt = prepare(config, condition, sources_url, private_env_path)
    trial_identity = identity(config, condition, probe, catalog_identity, receipt)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codex-reference-home-") as home_raw, tempfile.TemporaryDirectory(
        prefix="codex-reference-cwd-"
    ) as cwd_raw:
        home, cwd = Path(home_raw), Path(cwd_raw)
        home.chmod(0o700)
        cwd.chmod(0o700)
        env = native._child_env(home, native._sanitized_chatgpt_auth(root))
        catalog_path, schema_path = home / "catalog.json", cwd / "schema.json"
        codex_catalog.write_catalog(catalog_path, catalog)
        schema = adapters.response_schema(packet)
        codex_catalog.write_catalog(schema_path, schema)
        trial_identity["response_schema_sha256"] = adapters.digest(schema)
        journal_path = home / "reference-journal.jsonl"
        overrides = ()
        if condition == "sources":
            bridge_config = home / "reference.json"
            codex_catalog.write_catalog(bridge_config, {
                "url": sources_url, "tools": config["tools"], "cap": config["max_tool_calls"],
                "timeout": min(config["timeout_seconds"], 20), "schemas": receipt["schemas"],
                "server_sha256": receipt["source_server_sha256"], "journal": str(journal_path),
            })
            overrides = reference_overrides(bridge_config, config["tools"])
        argv = codex_catalog.build_argv(str(probe.binary_path), model=config["model"], effort=config["effort"],
                                        response_schema_path=schema_path, catalog_path=catalog_path,
                                        reference_overrides=overrides)
        if evidence is not None:
            evidence("cli_invocation", {"argv": argv, "cwd": str(cwd), "env_keys": sorted(env),
                                        "condition": condition, **trial_identity})
        journal = []
        try:
            completed = native._run_process(argv, cwd=cwd, env=env, prompt=prompt,
                                             timeout=config["timeout_seconds"], evidence=evidence)
        finally:
            if journal_path.exists():
                if journal_path.stat().st_size > native._MAX_OUTPUT_BYTES:
                    raise native._fail("native reference journal exceeds bound")
                raw = journal_path.read_text()
                if evidence is not None:
                    evidence("reference_controller", {"journal": raw})
                journal = [adapters._strict_json_loads(line) for line in raw.splitlines()]
        if evidence is not None:
            evidence("cli_result", {"returncode": completed.returncode,
                                     "stdout": completed.stdout, "stderr": completed.stderr})
        if completed.returncode != 0:
            raise native._fail("Codex CLI reference invocation failed")
        if condition == "sources":
            ready = [entry for entry in journal if entry.get("event") == "ready"]
            if not ready or any(entry["tools_sha256"] != trial_identity["tool_schema_sha256"]
                                or entry["server_sha256"] != receipt["source_server_sha256"] for entry in ready):
                raise native._fail("native reference initialization evidence missing or changed")
        parsed, calls = parse_events(completed.stdout, packet, config["tools"] if condition == "sources" else [], journal)
    trial_identity["session_id"] = parsed.session_id
    trial = {"responses": parsed.responses, "identity": trial_identity,
             "metrics": {"elapsed_seconds": time.monotonic() - started, **parsed.usage, "tool_calls": calls}}
    if parsed.answer_failure_reason is not None:
        trial.update(status="failed", failure_reason=CANDIDATE_RESPONSE_ERROR,
                     responses={str(item["id"]): None for item in packet["items"]})
        if evidence is not None:
            evidence("candidate_answer_outcome", {"failure_reason": CANDIDATE_RESPONSE_ERROR,
                     "parser_reason": parsed.answer_failure_reason, "answer_content": parsed.answer_content,
                     "session_id": parsed.session_id, "tool_calls": calls, "usage": parsed.usage})
    return trial
