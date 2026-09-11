"""Native AGY controls; subprocess fixtures use no provider credentials."""

from __future__ import annotations

import copy
import hashlib
import json
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ukrainian_llm_eval import adapters, agy_hook
from ukrainian_llm_eval import native_agy as native


def config(**extra):
    return {"schema": "zno-nmt.config.v1", "adapter": "agy", "model": "gemini-3.8-flash-low", "effort": "low",
            "provider": native.PROVIDER, "timeout_seconds": 15, "max_output_tokens": 4096, "max_tool_calls": 1,
            "repeats": 1, "tools": ["verify_word"], "corpus_id": "fixture", "agy_bin": "fixture", **extra}


def packet():
    return {"schema": "zno-nmt.questions.v1", "packet_sha256": "a" * 64,
            "items": [{"id": "q1", "kind": "single", "question": "Fixture", "options": [{"id": "A", "text": "A"}], "rows": []}]}


def reference_args():
    return {"ServerName": "sources", "ToolName": "verify_word", "Arguments": {"word": "fixture"}}


def hook_receipts(sources=False):
    refs = [{"decision": "allow", "count_before": 0, "call": {"name": "call_mcp_tool", "args": reference_args()}}] if sources else []
    return [*refs, {"decision": "allow", "count_before": int(sources), "call": {"name": "finish", "args": {"responses": {"q1": "A"}, "toolSummary": "Fixture", "toolAction": "Return answer"}}}]


def call_receipts(sources=False):
    return [{"name": "verify_word", "arguments": {"word": "fixture"}, "result": {"content": [{"type": "text", "text": "REFERENCE"}]}}] if sources else []


def events(sources=False):
    schema = adapters.response_schema(packet())
    steps = [{"conversation_id": "session", "step_index": 0, "state": "DONE", "step_type": "user_input"}]
    if sources:
        steps.append({"conversation_id": "session", "step_index": 1, "state": "DONE", "step_type": "tool", "tool_name": "call_mcp_tool", "tool_info": {"name": "call_mcp_tool", "parameters": reference_args(), "output": "REFERENCE"}})
    steps.append({"conversation_id": "session", "step_index": 2, "state": "DONE", "step_type": "finish"})
    return [{"event": "init", "init": {"model": config()["model"], "agent": native.PROFILE_NAME, "json_schema": schema}},
            *[{"event": "step_update", "step_update": step} for step in steps],
            {"event": "result", "result": {"conversation_id": "session", "num_turns": 1, "status": "SUCCESS",
                                            "structured_output": {"responses": {"q1": "A"}}, "json_schema": schema,
                                            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}}]


def serialize(value):
    return "\n".join(json.dumps(item) for item in value)


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_exact_model_effort_config(effort):
    value = config(model="gemini-3.8-flash-" + effort, effort=effort)
    assert adapters.validate_config(value) == value
    with pytest.raises(adapters.AdapterError):
        adapters.validate_config(config(model="gemini-3.8-flash-other", effort=effort))


@pytest.mark.parametrize("extra", [{"provider": "gemini-api"}, {"key_env": "GEMINI_API_KEY"}, {"claude_bin": "wrong"}, {"effort": None}])
def test_paid_or_conflicting_routes_rejected(extra):
    with pytest.raises(adapters.AdapterError):
        native.validate_config(config(**extra))


def test_environment_excludes_keys_customizations_and_proxy(monkeypatch, tmp_path):
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL", "HTTP_PROXY", "PYTHONPATH", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "must-not-inherit")
    value = native.child_env(tmp_path)
    assert "must-not-inherit" not in value.values()
    assert value["HOME"] == str(tmp_path)
    assert native.settings(config(), "sources")["useG1Credits"] is False


def provision(tmp_path):
    root = tmp_path / "provision"
    root.mkdir(mode=0o700)
    token = root / native.AUTH_FILE
    token.write_text("synthetic-credential")
    token.chmod(0o600)
    return root


def test_only_owner_readable_regular_credentials(tmp_path):
    root = provision(tmp_path)
    assert native._credential(root) == b"synthetic-credential"
    (root / native.AUTH_FILE).chmod(0o644)
    with pytest.raises(adapters.AdapterError, match="owner-only"):
        native._credential(root)


def test_symlink_credentials_rejected(tmp_path):
    root = tmp_path / "provision"
    root.mkdir(mode=0o700)
    token = tmp_path / "actual"
    token.write_text("fixture")
    token.chmod(0o600)
    (root / native.AUTH_FILE).symlink_to(token)
    with pytest.raises(adapters.AdapterError, match="owner-only"):
        native._credential(root)


@pytest.mark.parametrize("name", ["run_command", "manage_task", "read_resource", "search_web", "invoke_subagent", "send_message", "finish_typo"])
def test_hook_denies_non_reference_actions(name):
    controls = {"tools": ["verify_word"], "max_tool_calls": 1, "deadline": time.monotonic() + 10}
    assert agy_hook.decide({"name": name, "args": {}}, controls, 0) == (False, False)


def test_hook_closed_book_limit_and_expiry():
    call = {"name": "call_mcp_tool", "args": reference_args()}
    controls = {"tools": ["verify_word"], "max_tool_calls": 1, "deadline": time.monotonic() + 10}
    assert agy_hook.decide(call, controls, 0) == (True, True)
    assert agy_hook.decide(call, controls, 1) == (False, True)
    assert agy_hook.decide(call, {**controls, "tools": []}, 0) == (False, False)
    assert agy_hook.decide(call, {**controls, "deadline": 0}, 0) == (False, False)


def test_parallel_hooks_cannot_exceed_total_call_cap(tmp_path):
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({"tools": ["verify_word"], "max_tool_calls": 1, "deadline": time.monotonic() + 10}))
    def invoke(_index):
        result = subprocess.run([sys.executable, agy_hook.__file__, str(gate)], input=json.dumps({"toolCall": {"name": "call_mcp_tool", "args": reference_args()}}), text=True, capture_output=True, check=True)
        return json.loads(result.stdout)["decision"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(invoke, range(4)))
    assert results.count("allow") == 1 and results.count("deny") == 3
    assert gate.with_suffix(".state").read_text() == "1"


@pytest.mark.parametrize("sources", [False, True])
def test_complete_native_evidence(sources):
    result = native.parse_events(serialize(events(sources)), packet(), config(), hook_receipts(sources), call_receipts(sources))
    assert result["responses"] == {"q1": "A"} and result["metrics"]["tool_calls"] == int(sources)
    assert result["metrics"]["cost_usd"] is None


@pytest.mark.parametrize("failure", ["model", "schema", "final", "extra_turn", "tool_error", "subagent", "output", "arguments", "unfinished", "missing_step"])
def test_native_drift_or_failure_never_becomes_success(failure):
    value = events(True)
    if failure == "model": value[0]["init"]["model"] = "other"
    elif failure == "schema": value[-1]["result"]["json_schema"] = {}
    elif failure == "final": value[-1]["result"]["structured_output"] = '```json\n{"responses":{"q1":"A"}}\n```'
    elif failure == "extra_turn": value[-1]["result"]["num_turns"] = 2
    elif failure == "tool_error": value[2]["step_update"]["tool_info"]["error"] = {"type": "failure"}
    elif failure == "subagent": value[2]["step_update"]["subagent_info"] = {"subagents": [{}]}
    elif failure == "output": value[2]["step_update"]["tool_info"]["output"] = "FORGED"
    elif failure == "arguments": value[2]["step_update"]["tool_info"]["parameters"]["Arguments"] = {"word": "other"}
    elif failure == "unfinished": value[2]["step_update"]["state"] = "ACTIVE"
    elif failure == "missing_step": value.pop(2)
    with pytest.raises(adapters.AdapterError):
        native.parse_events(serialize(value), packet(), config(), hook_receipts(True), call_receipts(True))


@pytest.mark.parametrize("receipts", [[], [{"decision": "deny"}]])
def test_absent_or_denied_hooks_rejected(receipts):
    with pytest.raises(adapters.AdapterError, match="tool_policy_error"):
        native.parse_events(serialize(events()), packet(), config(), receipts, [])


def test_native_finish_matches_hook_payload():
    hooks = hook_receipts()
    hooks[0]["call"]["args"]["responses"]["q1"] = None
    with pytest.raises(adapters.AdapterError, match="structured evidence"):
        native.parse_events(serialize(events()), packet(), config(), hooks, [])


def test_native_runner_copies_only_credentials_and_one_prompt(monkeypatch, tmp_path):
    root = provision(tmp_path)
    (root / "untrusted-settings.json").write_text("DO NOT IMPORT")
    binary = tmp_path / "fixture"
    binary.write_text("fixture")
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    monkeypatch.setattr(native, "_binary", lambda _config: (str(binary), binary_hash))
    def run(argv, *, cwd, env, prompt, **_kwargs):
        assert json.loads(prompt) == {"event": "user", "message": {"content": "packet"}}
        home = Path(env["HOME"])
        assert (home / ".gemini/antigravity-cli" / native.AUTH_FILE).read_text() == "synthetic-credential"
        assert not list(home.rglob("untrusted-settings.json"))
        settings = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text())
        assert settings["useG1Credits"] is False and not settings["permissions"]["allow"]
        hooks = json.loads((home / ".gemini/config/hooks.json").read_text())
        command = hooks["evaluator-gate"]["PreToolUse"][0]["hooks"][0]["command"]
        gate = Path(shlex.split(command)[-1])
        gate.with_suffix(".jsonl").write_text(serialize(hook_receipts()))
        return subprocess.CompletedProcess(argv, 0, serialize(events()), "")
    monkeypatch.setattr(adapters, "_run_claude_process", run)
    result = native.run_agy(packet(), config(), "closed-book", sources_url=None, prompt="packet", private_env_path=root)
    assert result["responses"] == {"q1": "A"}
    assert result["identity"]["max_output_tokens_effective"] == "unknown"
    assert result["identity"]["effective_effort"] == "unknown"
    assert result["identity"]["g1_credit_fallback"] is False


def test_effort_changes_comparison():
    from ukrainian_llm_eval import runner
    before = runner._comparison(packet(), config())
    changed = copy.deepcopy(config())
    changed["effort"] = "high"
    changed["model"] = "gemini-3.8-flash-high"
    assert runner._comparison(packet(), changed) != before


def test_expired_reference_setup_never_launches_candidate(monkeypatch, tmp_path):
    root = provision(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(native, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(native, "_binary", lambda _config: ("fixture", "a" * 64))
    def tools(*_args):
        clock[0] = 16.0
        return [{"name": "verify_word"}], None
    monkeypatch.setattr(adapters, "_mcp_list_tools", tools)
    monkeypatch.setattr(adapters, "_run_claude_process", lambda *_args, **_kwargs: pytest.fail("candidate must not launch"))
    with pytest.raises(adapters.AdapterError, match="timeout before"):
        native.run_agy(packet(), config(), "sources", sources_url="http://localhost:1/mcp", prompt="packet", private_env_path=root)
