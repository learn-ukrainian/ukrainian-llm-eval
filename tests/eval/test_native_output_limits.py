"""Credential-free output-selection contracts, never native-runtime proof."""

import contextlib
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ukrainian_llm_eval import adapters, codex_reference, core, native_agy, native_codex, native_cursor, runner

MODELS = [
    ("codex", "gpt-6.1-sol", native_codex.CODEX_PROVIDER),
    ("codex", "gpt-6-luna", native_codex.CODEX_PROVIDER),
    ("agy", "gemini-3.8-flash-high", native_agy.PROVIDER),
    ("cursor", "grok-4.7-high", native_cursor.CURSOR_PROVIDER),
    ("claude", "claude-sonnet-5-5", "claude-cli"),
    ("claude", "claude-opus-5-5", "claude-cli"),
]
CONDITIONS = ["closed-book", "sources"]
SELECTIONS = ["native-default", 8192, 12345]
CATALOG = [{"name": "verify_word", "description": "Synthetic reference",
            "inputSchema": {"type": "object", "properties": {"word": {"type": "string"}}}}]
WIRE = {"responses": {"q0001": {"answer": "A", "explanation": "Synthetic evidence."}}}
USAGE = {"input_tokens": 10000, "output_tokens": 20000, "total_tokens": 30000}


def config(route, selection="native-default"):
    adapter, model, provider = route
    value = {"schema": "zno-nmt.config.v1", "adapter": adapter, "model": model, "provider": provider,
             "effort": "high", "timeout_seconds": 600, "max_output_tokens": selection,
             "max_tool_calls": 20, "repeats": 1, "tools": ["verify_word"], "corpus_id": "synthetic"}
    if adapter == "codex":
        value["codex_tool_policy"] = "reference-only"
    return value


def packet():
    body = {"schema": "zno-nmt.questions.v1", "items": [{"id": "q0001", "kind": "single",
            "question": "Synthetic choice", "options": [{"id": "A", "text": "Synthetic option"}], "rows": []}]}
    return {**body, "packet_sha256": adapters.digest(body)}


def assert_selection(identity, route, selection):
    expected = ("runtime-default" if selection == "native-default" else
                "environment-requested" if route[0] == "claude" else "numeric-metadata-not-forwarded")
    assert identity["max_output_tokens_configured"] == selection
    assert identity["max_output_tokens_effective"] == "unknown"
    assert identity["max_output_tokens_mechanism"] == expected


@pytest.fixture
def synthetic_runtime(tmp_path, monkeypatch):
    """Stub external boundaries while exercising real argv, parsing and receipts."""
    binary = tmp_path / "inert-runtime"
    binary.write_text("inert synthetic artifact; never executed")
    binary.chmod(0o700)
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    captured = []
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "77777")
    monkeypatch.setattr(adapters, "_managed_inventory", lambda: {})
    monkeypatch.setattr(adapters, "_mcp_list_tools", lambda *_: (copy.deepcopy(CATALOG), "d" * 64))
    def inherited_override(*_):
        adapters._ATTEMPT.get()["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "77777"
        return None

    monkeypatch.setattr(adapters, "_provision_claude", inherited_override)
    monkeypatch.setattr(adapters, "_auth_integrity", lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(adapters, "_claude_auth_contract", lambda *_: None)
    monkeypatch.setattr(adapters, "_controller_evidence", lambda *_: ([], 0))
    monkeypatch.setattr(native_cursor, "_provision_cursor", lambda *_: None)
    monkeypatch.setattr(native_cursor, "_auth_contract", lambda *_: None)
    monkeypatch.setattr(native_cursor, "_assert_login", lambda *_: None)
    monkeypatch.setattr(native_cursor, "_probe_cli", lambda *_: native_cursor._CliProbe(
        str(binary), binary, binary_hash, "synthetic-version"))
    monkeypatch.setattr(native_agy, "_credential", lambda *_: b"synthetic")
    monkeypatch.setattr(native_agy, "_binary", lambda *_: (str(binary), binary_hash))
    monkeypatch.setattr(native_agy, "capture_native_artifacts", lambda *_: "synthetic-session")
    reference = SimpleNamespace(url="http://reference.invalid", token="synthetic", calls=[], metadata=[], error=None)
    monkeypatch.setattr(native_agy, "ReferenceServer", lambda *_: contextlib.nullcontext(reference))
    monkeypatch.setattr(native_agy.subprocess, "run", lambda argv, **_: subprocess.CompletedProcess(argv, 0, "", ""))
    probe = native_codex._CliProbe(str(binary), binary, "e" * 64, binary, "f" * 64, "synthetic-version")
    monkeypatch.setattr(native_codex, "_sanitized_chatgpt_auth", lambda *_: b"{}")
    monkeypatch.setattr(codex_reference, "bridge_command", lambda: binary)

    def prepare(value, condition, *_):
        checked = adapters.validate_config(value)
        receipt = {"schemas": CATALOG, "source_server_sha256": "d" * 64}
        return checked, tmp_path, probe, {"models": []}, {"catalog_tool_policy_sha256": "c" * 64}, receipt

    monkeypatch.setattr(codex_reference, "prepare", prepare)

    def capability(argv, **_):
        captured.append(("probe", argv, dict(adapters._ATTEMPT.get()["env"])))
        output = " ".join([*adapters._CLAUDE_FLAGS, "--allowedTools"]) if "--help" in argv else "synthetic-version"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(adapters, "_run_checked", capability)

    def execute(argv, *, cwd, env, prompt, **_):
        captured.append(("execution", argv, dict(env), prompt))
        model = argv[argv.index("--model") + 1]
        if "--agent" in argv:
            schema = adapters.response_schema(packet())
            finish = {**WIRE, "toolSummary": "Synthetic", "toolAction": "Return answer"}
            (cwd.parent / "reference-gate.jsonl").write_text(json.dumps(
                {"decision": "allow", "count_before": 0, "call": {"name": "finish", "args": finish}}) + "\n")
            events = [
                {"event": "init", "init": {"model": model, "agent": native_agy.PROFILE_NAME, "json_schema": schema}},
                *[{"event": "step_update", "step_update": {"conversation_id": "synthetic-session", "step_index": index,
                    "state": "DONE", "step_type": kind}} for index, kind in enumerate(["user_input", "finish"])],
                {"event": "result", "result": {"conversation_id": "synthetic-session", "num_turns": 1,
                    "status": "SUCCESS", "structured_output": WIRE, "json_schema": schema, "usage": USAGE}},
            ]
        elif "--output-last-message" in argv:
            final = json.dumps(WIRE)
            Path(argv[argv.index("--output-last-message") + 1]).write_text(final)
            controller_path = cwd.parent / "home/reference.json"
            if controller_path.exists():
                controller = json.loads(controller_path.read_text())
                journal = Path(controller["journal"])
                journal.write_text(json.dumps({"event": "ready", "tools_sha256": adapters.digest(CATALOG),
                                              "server_sha256": "d" * 64}) + "\n")
                journal.chmod(0o600)
            events = [{"type": "thread.started", "thread_id": "synthetic-session"}, {"type": "turn.started"},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
                      {"type": "turn.completed", "usage": USAGE}]
        elif "--restricted" in argv:
            tools = ["StructuredOutput"]
            if "--allowedTools" in argv:
                tools.append("mcp__sources__verify_word")
            events = [{"type": "system", "subtype": "init", "model": model, "tools": tools,
                       "session_id": "synthetic-session"},
                      {"type": "tool_use", "name": "StructuredOutput", "input": WIRE},
                      {"type": "result", "result": json.dumps(WIRE), "structured_output": WIRE,
                       "session_id": "synthetic-session", "usage": USAGE}]
        else:
            events = [{"type": "system", "subtype": "init", "model": "Grok 4.7 High", "apiKeySource": "login",
                       "session_id": "synthetic-session"},
                      {"type": "assistant", "session_id": "synthetic-session", "message": {
                          "role": "assistant", "content": [{"type": "text", "text": json.dumps(WIRE)}]}},
                      {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(WIRE),
                       "session_id": "synthetic-session", "usage": USAGE}]
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(event) for event in events), "")

    monkeypatch.setattr(adapters, "_run_claude_process", execute)
    monkeypatch.setattr(native_codex, "_run_process", execute)
    monkeypatch.setattr(native_cursor, "_run_cursor_process", execute)
    return captured


@pytest.mark.parametrize("route", MODELS, ids=[model for _, model, _ in MODELS])
@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("selection", SELECTIONS)
def test_six_models_both_conditions_receipts_invocation_usage(route, condition, selection, synthetic_runtime):
    value = config(route, selection)
    original = copy.deepcopy(value)
    evidence = []
    result = runner.run_exam(packet(), value, condition,
                             sources_url="https://reference.invalid/mcp" if condition == "sources" else None,
                             evidence=lambda name, payload: evidence.append((name, payload)))
    assert result["status"] == "ok", result
    assert result["responses"] == {"q0001": "A"}
    assert value == original
    assert_selection(result["identity"], route, selection)
    preflight = next(payload for name, payload in evidence if name == "preflight")
    assert_selection(preflight, route, selection)
    assert result["metrics"]["input_tokens"] == USAGE["input_tokens"]
    assert result["metrics"]["output_tokens"] == USAGE["output_tokens"]
    assert result["metrics"]["total_tokens"] == (None if route[0] == "codex" else USAGE["total_tokens"])
    assert result["metrics"]["tool_calls"] == 0
    if route[0] != "claude":
        assert result["identity"]["capture_stdout_max_bytes"] == 2_000_000
        assert preflight["capture_stdout_max_bytes"] == 2_000_000
    executions = [entry for entry in synthetic_runtime if entry[0] == "execution"]
    assert len(executions) == 1
    for entry in synthetic_runtime:
        _, argv, env, *_ = entry
        assert not any("max_output" in arg or "max-output" in arg for arg in argv)
        if route[0] == "claude" and selection != "native-default":
            assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(selection)
        else:
            assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env
    if route[0] == "claude":
        assert len([entry for entry in synthetic_runtime if entry[0] == "probe"]) == 4


@pytest.mark.parametrize("route", MODELS, ids=[model for _, model, _ in MODELS])
@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("selection", [None, True, False, 0, -1, 1.5, "8192", "default", "NATIVE-DEFAULT", "native-default "])
def test_invalid_selection_never_executes(route, condition, selection, monkeypatch):
    monkeypatch.setattr(runner, "preflight", lambda *_: pytest.fail("invalid selection reached preflight"))
    with pytest.raises(core.ExamError, match="max_output_tokens"):
        runner.run_exam(packet(), config(route, selection), condition)


@pytest.mark.parametrize("route", MODELS)
def test_output_selection_required(route):
    value = config(route)
    del value["max_output_tokens"]
    with pytest.raises(adapters.AdapterError):
        adapters.validate_config(value)


@pytest.mark.parametrize("route", MODELS)
@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("selection", SELECTIONS)
@pytest.mark.parametrize("stage", ["preflight", "execution"])
def test_failures_bind_requested_selection(route, condition, selection, stage, synthetic_runtime, monkeypatch):
    def fail(*_, **__):
        raise adapters.AdapterError("synthetic failure")
    if stage == "preflight":
        monkeypatch.setattr(runner, "preflight", fail)
    else:
        for module, name in [(adapters, "_run_claude_process"), (native_codex, "_run_process"),
                             (native_cursor, "_run_cursor_process")]:
            monkeypatch.setattr(module, name, fail)
    result = runner.run_exam(packet(), config(route, selection), condition,
                             sources_url="https://reference.invalid/mcp" if condition == "sources" else None)
    assert result["status"] == "failed"
    assert result["responses"] == {"q0001": None}
    assert_selection(result["identity"], route, selection)
    assert result["metrics"]["output_tokens"] is None


@pytest.mark.parametrize("route", MODELS)
def test_pair_selection_mismatch_is_incomparable(route, monkeypatch):
    # Isolate the pairing gate without reading or constructing grading keys.
    monkeypatch.setattr(core, "validate_key", lambda *_: None)
    monkeypatch.setattr(core, "_validate_run", lambda _, value: value)
    values = [runner._comparison(packet(), config(route, selection)) for selection in SELECTIONS]
    assert len({value["constants_sha256"] for value in values}) == len(SELECTIONS)
    for selection in SELECTIONS[1:]:
        control = {"condition": "closed-book", "status": "ok", "comparison": values[0]}
        treatment = {"condition": "sources", "status": "ok",
                     "comparison": runner._comparison(packet(), config(route, selection))}
        with pytest.raises(core.ExamError, match="different comparison configuration"):
            core.compare_runs(packet(), {}, control, treatment)


@pytest.mark.parametrize("adapter", ["chat-http", "responses-http", "opencode", "kimi"])
def test_other_adapters_reject_sentinel_preserve_integers(adapter):
    value = config((adapter, "synthetic-model", "synthetic-provider"))
    if adapter == "kimi":
        value.update(model="kimi-code/k2.5", provider="managed:kimi-code")
    else:
        value.update(effort=None, endpoint_env="SYNTHETIC_ENDPOINT", key_env="SYNTHETIC_KEY")
        if adapter == "opencode":
            value.update(model="google/gemma-4-31b-it", provider="openrouter",
                         openrouter={"provider_endpoint": "venice/bf16", "expected_provider_name": "Venice",
                                     "reasoning_enabled": False})
    with pytest.raises(adapters.AdapterError, match="max_output_tokens"):
        adapters.validate_config(value)
    value["max_output_tokens"] = 8192
    assert adapters.validate_config(value)["max_output_tokens"] == 8192
    assert adapters.native_output_limit_metadata(value) == {}


def test_claude_default_removes_override_already_in_attempt():
    # Test removal itself, not merely the ambient environment allowlist.
    with adapters._native_attempt() as attempt:
        attempt["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "77777"
        assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in adapters._child_env("native-default")
        assert adapters._child_env(12345)["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "12345"
        assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in adapters._child_env("native-default")
    with pytest.raises(adapters.AdapterError, match="native attempt boundary missing"):
        adapters._child_env("native-default")


@pytest.mark.parametrize("route", [MODELS[0], MODELS[1], MODELS[3]])
@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("selection", SELECTIONS)
def test_candidate_failure_keeps_usage_and_selection(route, condition, selection, synthetic_runtime, monkeypatch):
    module, name = ((native_codex, "_run_process") if route[0] == "codex" else
                    (native_cursor, "_run_cursor_process"))
    execute = getattr(module, name)

    def malformed(argv, **kwargs):
        result = execute(argv, **kwargs)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        for event in events:
            if event.get("type") == "item.completed":
                event["item"]["text"] = "Malformed synthetic answer"
            if event.get("type") == "assistant":
                event["message"]["content"][0]["text"] = "Malformed synthetic answer"
            if event.get("type") == "result":
                event["result"] = "Malformed synthetic answer"
        if "--output-last-message" in argv:
            Path(argv[argv.index("--output-last-message") + 1]).write_text("Malformed synthetic answer")
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(event) for event in events), "")

    monkeypatch.setattr(module, name, malformed)
    result = runner.run_exam(packet(), config(route, selection), condition,
                             sources_url="https://reference.invalid/mcp" if condition == "sources" else None)
    assert result["status"] == "failed"
    assert result["failure_reason"] == "candidate_response_error"
    assert result["responses"] == {"q0001": None}
    assert_selection(result["identity"], route, selection)
    assert result["metrics"]["output_tokens"] == USAGE["output_tokens"]
    assert result["metrics"]["input_tokens"] == USAGE["input_tokens"]


@pytest.mark.parametrize("route", MODELS[:2])
@pytest.mark.parametrize("selection", SELECTIONS)
def test_codex_original_closed_book_path_receipts(route, selection, synthetic_runtime, tmp_path, monkeypatch):
    value = config(route, selection)
    value.pop("codex_tool_policy")
    value.update(tools=[], corpus_id=None)
    probe = native_codex._CliProbe("synthetic", tmp_path, "e" * 64, tmp_path, "f" * 64, "synthetic-version")
    monkeypatch.setattr(native_codex, "validate_options", lambda value, **_: SimpleNamespace(
        config=adapters.validate_config(value), binary="synthetic", private_env_path=tmp_path))
    monkeypatch.setattr(native_codex, "_probe_cli", lambda *_: probe)
    monkeypatch.setattr(native_codex, "_load_control", lambda *_: {})

    def execute(argv, **_):
        events = [{"type": "thread.started", "thread_id": "synthetic-session"}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(WIRE)}},
                  {"type": "turn.completed", "usage": USAGE}]
        assert not any("max_output" in arg or "max-output" in arg for arg in argv)
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(event) for event in events), "")

    monkeypatch.setattr(native_codex, "_run_process", execute)
    preflight = native_codex.preflight_codex(value, "closed-book")
    result = native_codex.run_codex(packet(), value, "closed-book", sources_url=None, prompt="Synthetic")
    assert_selection(preflight, route, selection)
    assert_selection(result["identity"], route, selection)
    assert result["metrics"]["output_tokens"] == USAGE["output_tokens"]
