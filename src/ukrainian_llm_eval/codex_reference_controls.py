"""Capture credential-free local controls for the explicit reference policy.

Live Sources is read only for its tool/server metadata. Model responses and
reference answers in the control cases are synthetic loopback fixtures.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import os
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import adapters, codex_catalog, codex_controls, codex_reference, native_codex
from .codex_reference_bridge import snapshot

SERVER_INFO = {"name": "synthetic-reference", "version": "1"}
_RESOURCE_TOOLS = ["list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"]


def surface_matches(summary: dict, tools: list[str], closed: bool) -> bool:
    expected = {} if closed else {"functions": _RESOURCE_TOOLS, "mcp__sources": tools}
    namespaces = summary.get("additional_tool_namespaces")
    return (summary.get("tool_surface_valid") is True and summary.get("top_level_tool_count") == 0
            and isinstance(namespaces, dict) and set(namespaces) == set(expected)
            and all(len(namespaces[key]) == len(names) and set(namespaces[key]) == set(names)
                    for key, names in expected.items()))


def _events(action, model, index):
    events = codex_controls._tool_events(action, model)
    for _kind, event in events:
        for item in codex_controls._walk(event):
            if isinstance(item, dict):
                if item.get("type") == "function_call":
                    item["namespace"] = action.namespace
                if item.get("call_id") == codex_controls._CALL_ID:
                    item["call_id"] += f"_{index}"
    return events


class _Handler(codex_controls._LoopbackHandler):
    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 1 or size > codex_controls._MAX_BODY_BYTES:
            self._send_json(413, {"error": "synthetic body rejected"})
            return
        raw = self.rfile.read(size)
        decoded = codex_controls._decode_body(raw)
        data = adapters._strict_json_loads(decoded.decode())
        if self.path == "/mcp":
            method, ident = data.get("method"), data.get("id")
            self.server.mcp_methods.append(method)
            if method == "initialize":
                body = {"protocolVersion": data["params"]["protocolVersion"], "serverInfo": SERVER_INFO,
                        "capabilities": {"tools": {}}}
            elif method == "tools/list":
                body = {"tools": self.server.schemas}
            elif method == "tools/call":
                self.server.mcp_calls.append(data["params"])
                body = {"content": [{"type": "text", "text": "SYNTHETIC_REFERENCE_OK"}]}
            elif method == "notifications/initialized":
                self._send_json(200, {})
                return
            else:
                self._send_json(200, {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601}})
                return
            self._send_json(200, {"jsonrpc": "2.0", "id": ident, "result": body})
            return
        state = self._state()
        codex_controls._write_new(state.root / f"request-{len(state.posts):03d}.bin", raw)
        summary = codex_controls._request_summary(raw)
        state.posts.append(summary)
        outputs = [value for value in codex_controls._walk(data) if isinstance(value, dict)
                   and value.get("type") == "function_call_output"]
        state.tool_outputs[:] = outputs
        valid = surface_matches(summary, self.server.allowed, self.server.closed)
        self.server.surface_valid &= valid
        index = len(state.posts) - 1
        # Never inject a tool call into a surface that failed its allowlist.
        if valid and index < len(self.server.actions):
            self._stream(_events(self.server.actions[index], self.server.model, index))
        else:
            self._stream(codex_controls._final_events(self.server.model))


def _actions(case: str, tool: str, cap: int):
    def action(namespace, name, arguments):
        return codex_controls._HandlerCase(case, namespace, name, "function", adapters.canonical(arguments))
    if case == "denied-tool":
        return [action("mcp__sources", "forbidden_reference", {})]
    if case in {"closed-book", "reference-call", "call-cap", "schema-drift", "missing-tool"}:
        return [action("mcp__sources", tool, {"word": "synthetic"})
                for _ in range(cap + 1 if case == "call-cap" else 1)]
    names = {"resource-list": "list_mcp_resources", "resource-templates": "list_mcp_resource_templates",
             "resource-read": "read_mcp_resource"}
    arguments = {"server": "sources"}
    if case == "resource-read":
        arguments["uri"] = "synthetic://denied"
    return [action("functions", names[case], arguments)]


def capture_case(root: Path, config: dict, probe, catalog, schemas: list[dict], case: str) -> dict:
    root.mkdir(mode=0o700)
    actions = _actions(case, config["tools"][0], config["max_tool_calls"])
    state = codex_controls._FixtureState(root, actions[0], [], [], [])
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state, server.model, server.schemas = state, config["model"], copy.deepcopy(schemas)
    if case == "schema-drift":
        server.schemas[0]["description"] = "Synthetic schema drift"
    if case == "missing-tool":
        server.schemas = []
    server.allowed, server.closed, server.actions = config["tools"], case == "closed-book", actions
    server.mcp_methods, server.mcp_calls, server.surface_valid = [], [], True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    journal = []
    try:
        with tempfile.TemporaryDirectory(prefix="codex-ref-control-") as temp:
            home = Path(temp)
            cwd = home / "cwd"
            cwd.mkdir(mode=0o700)
            env = {key: value for key, value in os.environ.items() if key in native_codex._SAFE_CHILD_ENV}
            env.update(HOME=str(home), CODEX_HOME=str(home / "codex-home"))
            Path(env["CODEX_HOME"]).mkdir(mode=0o700)
            schema_path, catalog_path, config_path = home / "schema.json", home / "catalog.json", home / "reference.json"
            codex_catalog.write_catalog(catalog_path, catalog)
            codex_catalog.write_catalog(schema_path, adapters.response_schema(codex_controls._SYNTHETIC_PACKET))
            journal_path = root / "journal.jsonl"
            base = f"http://127.0.0.1:{server.server_address[1]}"
            codex_catalog.write_catalog(config_path, {"url": base + "/mcp", "tools": config["tools"],
                "cap": config["max_tool_calls"], "timeout": 3, "schemas": schemas,
                "server_sha256": adapters.digest(SERVER_INFO), "journal": str(journal_path)})
            argv = codex_catalog.build_argv(str(probe.binary_path), model=config["model"], effort=config["effort"],
                response_schema_path=schema_path, catalog_path=catalog_path,
                reference_overrides=() if server.closed else codex_reference.reference_overrides(config_path, config["tools"]),
                transport_overrides=codex_controls._loopback_overrides(base + "/v1"))
            result = native_codex._run_process(argv, cwd=cwd, env=env, prompt="Synthetic control.",
                timeout=min(config["timeout_seconds"], 60), evidence=lambda name, value: codex_controls._write_json(
                    root / f"process-{name}.json", value))
            codex_controls._write_new(root / "stdout.bin", result.stdout.encode())
            codex_controls._write_new(root / "stderr.bin", result.stderr.encode())
            if journal_path.exists():
                journal = [adapters._strict_json_loads(line) for line in journal_path.read_text().splitlines()]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    expected_calls = 1 if case == "reference-call" else config["max_tool_calls"] if case == "call-cap" else 0
    text = adapters.canonical(state.tool_outputs)
    outcome = ("SYNTHETIC_REFERENCE_OK" in text if expected_calls else bool(state.tool_outputs))
    if case == "closed-book":
        outcome = "unsupported call:" in text
    if case == "denied-tool":
        outcome = "unsupported call:" in text
    if case == "call-cap":
        outcome &= "Reference call limit reached" in text
    passed = (result.returncode == 0 and server.surface_valid and len(state.posts) == len(actions) + 1
              and len(server.mcp_calls) == expected_calls and outcome
              and set(server.mcp_methods) <= {"initialize", "notifications/initialized", "tools/list", "tools/call"})
    if not server.closed:
        passed &= any(entry.get("event") == "ready" for entry in journal)
    if case in {"schema-drift", "missing-tool"}:
        passed = result.returncode != 0 and not state.posts and not server.mcp_calls
    report = {"case": case, "passed": bool(passed), "scope": "local synthetic fixture; no credentials or provider inference",
              "model": config["model"], "effort": config["effort"], "requests": state.posts,
              "upstream_reference_calls": len(server.mcp_calls), "upstream_methods": server.mcp_methods,
              "tool_outputs": state.tool_outputs, "controller_journal": journal,
              "returncode": result.returncode}
    path = root / "capture.json"
    codex_controls._write_json(path, report)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "passed": bool(passed)}


def capture(config: dict, schemas: list[dict], source_server_sha256: str, output: Path) -> dict:
    config = native_codex.validate_config(config)
    if config.get("codex_tool_policy") != "reference-only" or config["max_tool_calls"] > 100:
        raise ValueError("controls require reference-only policy and a bounded call cap <= 100")
    output.mkdir(mode=0o700)
    probe = native_codex._probe_cli(config["codex_bin"], config["timeout_seconds"])
    catalog, catalog_identity = codex_catalog.bundled_catalog(probe, config["model"], config["timeout_seconds"])
    captures = {case: capture_case(output / case, config, probe, catalog, schemas, case)
                for case in codex_reference.CASES}
    passed = all(entry["passed"] for entry in captures.values())
    receipt = {"schema": codex_reference.CONTROL_SCHEMA, "status": "passed" if passed else "failed",
        "model": config["model"], "effort": config["effort"], "tools": config["tools"],
        "max_tool_calls": config["max_tool_calls"], "schemas": schemas, "source_server_sha256": source_server_sha256,
        "entrypoint_sha256": probe.entrypoint_sha256, "native_runtime_sha256": probe.native_runtime_sha256,
        "cli_version": probe.version, "implementation_sha256": codex_reference.implementation_hash(),
        "catalog_identity": catalog_identity,
        "bridge_entrypoint_sha256": hashlib.sha256(codex_reference.bridge_command().read_bytes()).hexdigest(),
        "captures": {case: {key: value for key, value in entry.items() if key != "passed"}
                     for case, entry in captures.items()}}
    codex_controls._write_json(output / "report.json", receipt)
    if passed:
        codex_controls._write_json(output / codex_reference.CONTROL_FILE, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--sources-url-env", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = native_codex.validate_config(adapters._strict_json_loads(args.config.read_text()))
        schemas, server = snapshot(os.environ[args.sources_url_env], config["tools"],
                                   config["timeout_seconds"], config["max_tool_calls"])
        result = capture(config, schemas, server, args.output.resolve())
    except Exception:  # noqa: BLE001 - do not print private endpoint diagnostics
        print("Native reference control capture failed; no readiness claim")
        return 2
    print(adapters.canonical({"status": result["status"], "model": result["model"], "effort": result["effort"]}))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
