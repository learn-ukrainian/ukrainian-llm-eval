"""Reference-policy boundary tests; never use credentials or provider inference."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from ukrainian_llm_eval import codex_catalog, native_codex
from ukrainian_llm_eval import codex_reference as adapter
from ukrainian_llm_eval import codex_reference_bridge as reference
from ukrainian_llm_eval.mcp_proxy import Bridge


def test_catalog_preserves_identity_instructions_and_efforts():
    original = {"slug": "gpt-6-astra", "base_instructions": "unchanged",
                "supported_reasoning_levels": ["low", "medium", "high"],
                **{key: "enabled" for key in codex_catalog.TOOL_POLICY}}
    before = copy.deepcopy(original)
    result, identity = codex_catalog.restrict_catalog({"models": [original]}, "gpt-6-astra")
    assert original == before
    assert result["models"][0] == {**before, **codex_catalog.TOOL_POLICY}
    assert identity["bundled_model_entry_sha256"] != identity["restricted_model_entry_sha256"]
    with pytest.raises(Exception, match="missing, ambiguous"):
        codex_catalog.restrict_catalog({"models": [original, original]}, "gpt-6-astra")
    with pytest.raises(Exception, match="missing, ambiguous"):
        codex_catalog.restrict_catalog({"models": [original]}, "gpt-6-other")
    del original["tool_mode"]
    with pytest.raises(Exception, match="unsupported"):
        codex_catalog.restrict_catalog({"models": [original]}, "gpt-6-astra")


@pytest.fixture
def upstream(monkeypatch):
    observed = []
    tools = [{"name": "verify_word", "description": "synthetic",
              "inputSchema": {"type": "object", "properties": {"word": {"type": "string"}}}}]

    def request(self, method, params, ident=1):
        observed.append((method, params))
        if method == "initialize":
            body = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "synthetic", "version": "1"}}
        elif method == "tools/list":
            body = {"tools": copy.deepcopy(tools)}
        elif method == "tools/call":
            body = {"content": [{"type": "text", "text": "synthetic"}]}
        else:
            return {}
        return {"jsonrpc": "2.0", "id": ident, "result": body}

    monkeypatch.setattr(Bridge, "request", request)
    return observed, tools


def initialize(bridge):
    bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})


def test_denied_methods_and_cap_never_reach_upstream(upstream, tmp_path):
    observed, tools = upstream
    journal = tmp_path / "journal"
    bridge = reference.ReferenceBridge("unused", ["verify_word"], timeout=1, max_tool_calls=1,
                                       expected_tools=tools, journal=journal)
    initialize(bridge)
    for method in ["resources/list", "resources/templates/list", "resources/read", "prompts/get"]:
        assert "error" in bridge.handle({"jsonrpc": "2.0", "id": 3, "method": method})
    assert "error" in bridge.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                      "params": {"name": "delete_data", "arguments": {}}})
    assert bridge.handle({"jsonrpc": "2.0", "method": "tools/call",
                          "params": {"name": "verify_word"}}) is None
    call = {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "verify_word", "arguments": {"word": "synthetic"}}}
    assert "result" in bridge.handle(call)
    assert "error" in bridge.handle(call)
    assert [method for method, _ in observed] == ["initialize", "tools/list", "tools/call"]
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len([event for event in events if event["event"] == "call"]) == 1
    assert events[-1]["reason"] == "call_cap"


def test_schema_drift_and_reinitialization_fail_closed(upstream):
    observed, tools = upstream
    bridge = reference.ReferenceBridge("unused", ["verify_word"], timeout=1, max_tool_calls=1,
                                       expected_tools=copy.deepcopy(tools))
    initialize(bridge)
    tools[0]["description"] = "changed model-visible instructions"
    with pytest.raises(ValueError, match="schema drift"):
        bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert not bridge.ready
    assert "error" in bridge.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                      "params": {"name": "verify_word"}})
    with pytest.raises(ValueError, match="repeated"):
        initialize(bridge)
    assert not any(method == "tools/call" for method, _ in observed)


def test_missing_duplicate_and_paginated_schemas_rejected(upstream, monkeypatch):
    _, tools = upstream
    with pytest.raises(ValueError, match="missing"):
        reference.normalized_tools([], ["verify_word"])
    with pytest.raises(ValueError, match="duplicate"):
        reference.normalized_tools(tools + tools, ["verify_word"])
    bridge = reference.ReferenceBridge("unused", ["verify_word"], timeout=1, max_tool_calls=1)
    bridge.initialized = True
    monkeypatch.setattr(bridge, "request", lambda *args: {"result": {"tools": tools, "nextCursor": "more"}})
    with pytest.raises(ValueError, match="incomplete"):
        bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


def _native_fixture(tmp_path, monkeypatch):
    from test_native_codex import _config, _probe

    config = native_codex.validate_config(_config(model="gpt-6-astra", effort="low", tools=["verify_word"],
                                                  codex_tool_policy="reference-only"))
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    (root / "auth.json").write_text("{}")
    (root / "auth.json").chmod(0o600)
    probe = _probe()
    catalog = {"models": [{"slug": "gpt-6-astra", **codex_catalog.TOOL_POLICY}]}
    catalog_identity = {"catalog_tool_policy_sha256": "a" * 64}
    schemas = [{"name": "verify_word", "inputSchema": {"type": "object"}}]
    captures = {}
    for case in adapter.CASES:
        path = tmp_path / f"{case}.json"
        path.write_text(json.dumps({"case": case, "passed": True}))
        captures[case] = {"path": str(path), "sha256": adapter.hashlib.sha256(path.read_bytes()).hexdigest()}
    receipt = {"schema": adapter.CONTROL_SCHEMA, "status": "passed", "model": config["model"],
        "effort": config["effort"], "tools": config["tools"], "max_tool_calls": config["max_tool_calls"],
        "entrypoint_sha256": probe.entrypoint_sha256, "native_runtime_sha256": probe.native_runtime_sha256,
        "cli_version": probe.version, "implementation_sha256": adapter.implementation_hash(),
        "catalog_identity": catalog_identity, "schemas": schemas, "source_server_sha256": "c" * 64,
        "bridge_entrypoint_sha256": adapter.hashlib.sha256(adapter.bridge_command().read_bytes()).hexdigest(),
        "captures": captures}
    control = root / adapter.CONTROL_FILE
    control.write_text(json.dumps(receipt))
    control.chmod(0o600)
    monkeypatch.setattr(native_codex, "_probe_cli", lambda *_: probe)
    monkeypatch.setattr(native_codex, "_sanitized_chatgpt_auth", lambda *_: b"{}")
    monkeypatch.setattr(codex_catalog, "bundled_catalog", lambda *_: (catalog, catalog_identity))
    monkeypatch.setattr(adapter, "snapshot", lambda *_: (schemas, "c" * 64))
    return config, root, receipt


@pytest.mark.parametrize("drift", ["schema", "server", "effort", "artifact"])
def test_reference_drift_rejected_before_inference(tmp_path, monkeypatch, drift):
    config, root, receipt = _native_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(native_codex, "_run_process", lambda *a, **kw: pytest.fail("inference must not start"))
    if drift == "schema":
        monkeypatch.setattr(adapter, "snapshot", lambda *_: ([], "c" * 64))
    elif drift == "server":
        monkeypatch.setattr(adapter, "snapshot", lambda *_: (receipt["schemas"], "d" * 64))
    elif drift == "effort":
        config["effort"] = "high"
    else:
        Path(receipt["captures"]["closed-book"]["path"]).write_text("{}")
    with pytest.raises(native_codex.CodexAdapterError, match="drift"):
        native_codex.preflight_codex(config, "sources", "https://reference.invalid/mcp", private_env_path=root)


@pytest.mark.parametrize("interrupted", [False, True])
def test_reference_run_preserves_failed_answers_and_partial_controller_evidence(tmp_path, monkeypatch, interrupted):
    from test_native_codex import _packet

    config, root, receipt = _native_fixture(tmp_path, monkeypatch)
    evidence = []

    def process(argv, **kwargs):
        assert "code_mode_host" in argv and argv[argv.index("code_mode_host") - 1] == "--disable"
        assert all("reference.invalid" not in value for value in argv)
        home = Path(kwargs["env"]["HOME"])
        controller = json.loads((home / "reference.json").read_text())
        assert controller["schemas"] == receipt["schemas"]
        arguments = {"word": "synthetic"}
        journal = [
            {"event": "ready", "tools_sha256": adapter.adapters.digest(receipt["schemas"]), "server_sha256": "c" * 64},
            {"event": "call", "index": 1, "tool": "verify_word", "arguments_sha256": adapter.adapters.digest(arguments)},
        ]
        if not interrupted:
            journal.append({"event": "result", "index": 1, "result_sha256": "e" * 64})
        Path(controller["journal"]).write_text("\n".join(json.dumps(entry) for entry in journal))
        if interrupted:
            raise native_codex.CodexAdapterError("fixture interruption")
        item = {"id": "call-1", "type": "mcp_tool_call", "server": "sources", "tool": "verify_word",
                "arguments": arguments, "status": "in_progress"}
        events = [
            {"type": "thread.started", "thread_id": "fixture-session"}, {"type": "turn.started"},
            {"type": "item.started", "item": item},
            {"type": "item.completed", "item": {**item, "status": "completed"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Malformed answer"}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}},
        ]
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(event) for event in events), "")

    monkeypatch.setattr(native_codex, "_run_process", process)
    kwargs = {"sources_url": "https://reference.invalid/mcp", "prompt": "Synthetic prompt", "private_env_path": root,
              "evidence": lambda name, value: evidence.append((name, value))}
    if interrupted:
        with pytest.raises(native_codex.CodexAdapterError, match="interruption"):
            native_codex.run_codex(_packet(), config, "sources", **kwargs)
    else:
        trial = native_codex.run_codex(_packet(), config, "sources", **kwargs)
        assert trial["status"] == "failed" and trial["responses"] == {"opaque-1": None}
        assert trial["metrics"]["tool_calls"] == 1
        assert trial["identity"]["effective_backend_model"] == "unknown"
        assert trial["identity"]["control_receipt_sha256"] == adapter.adapters.digest(receipt)
    assert any(name == "reference_controller" for name, _ in evidence)


def test_empty_closed_book_surface_and_extra_descriptor_rejection():
    from ukrainian_llm_eval.codex_reference_controls import surface_matches

    summary = {"tool_surface_valid": True, "top_level_tool_count": 0, "additional_tool_namespaces": {}}
    assert surface_matches(summary, ["verify_word"], True)
    summary["additional_tool_namespaces"] = {"collaboration": ["spawn_agent"]}
    assert not surface_matches(summary, ["verify_word"], True)
