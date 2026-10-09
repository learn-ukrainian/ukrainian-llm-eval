"""Actual fake-binary stdin captures across all six study routes (#69).

Only credential/control probes are replaced; real dispatch and wire extraction
run. This proves adapter contracts, not native isolation or provider behavior.
"""
from __future__ import annotations

import hashlib
import json
import sys

from native_validity_fixtures import staged_native_auth as staged_native_auth, synthetic_catalog as synthetic_catalog

import pytest
import test_answer_first_contract as contract
from answer_first_fixtures import wire_responses
from test_native_codex import _probe

from ukrainian_llm_eval import adapters, codex_reference, native_agy, native_codex, native_cursor, runner
from ukrainian_llm_eval.evidence import EvidenceStore

branch = contract.branch

ROUTES = [("codex", "gpt-6.1-sol"), ("codex", "gpt-6-luna"), ("agy", "gemini-3.8-flash-high"),
          ("cursor", "grok-4.7"), ("claude", "claude-sonnet-5-5"), ("claude", "claude-opus-5-5")]
# A frozen synthetic five-tool catalog; no live Sources discovery is involved.
TOOLS = ["verify_words", "verify_stress", "query_pravopys", "search_style_guide", "search_text"]
LISTING = [{"name": name, "inputSchema": {"type": "object"}, "description": "Synthetic reference " + name}
           for name in reversed(TOOLS)] + [{"name": "not_configured", "inputSchema": {"type": "object"}}]


def fake_binary(tmp_path, adapter, model, answers, *, wire=None):
    capture = tmp_path / "stdin.bin"
    schema_capture = tmp_path / "schema.json"
    binary = tmp_path / "candidate-fixture"
    script = '''
import hashlib, json, os, shlex, sys
from pathlib import Path
stdin = sys.stdin.buffer.read()
Path(CAPTURE).write_bytes(stdin)
argv = sys.argv[1:]
# Capture the actual child Sources configuration while its workspace exists.
if ADAPTER == 'claude':
    sources = json.loads(Path(argv[argv.index('--mcp-config')+1]).read_text())['mcpServers']
elif ADAPTER == 'agy':
    path = Path(os.environ['HOME'])/'.gemini/config/mcp_config.json'
    sources = json.loads(path.read_text())['mcpServers'] if path.exists() else {}
elif ADAPTER == 'cursor':
    path = Path.cwd()/'.cursor/mcp.json'
    sources = json.loads(path.read_text())['mcpServers'] if path.exists() else {}
else:
    sources = {'sources': True} if any(arg.startswith('mcp_servers.sources.args=') for arg in argv) else {}
Path(CAPTURE).with_suffix('.sources.json').write_text(json.dumps(sources))
if sources and ADAPTER in ('claude','cursor'):
    journal=Path.cwd().parent/'reference-journal.jsonl'
    journal.write_text(''); journal.chmod(0o600)
def emit(value):
    print(json.dumps(value, ensure_ascii=False))
schema = None
if '--json-schema' in argv:
    val = argv[argv.index('--json-schema') + 1]
    schema = json.loads(val) if ADAPTER == 'claude' else json.loads(Path(val).read_text())
elif '--output-schema' in argv:
    schema = json.loads(Path(argv[argv.index('--output-schema') + 1]).read_text())
if schema is not None:
    Path(SCHEMA_CAPTURE).write_text(json.dumps(schema))
raw = json.dumps(WIRE, ensure_ascii=False)
if ADAPTER == 'claude':
    prompt = stdin.decode()
    emit({'type':'system','subtype':'init','model':MODEL,'session_id':'fixture',
          'tools':['StructuredOutput'] + (['mcp__sources__'+name for name in TOOLS] if 'TRUSTED REFERENCE CATALOG' in prompt else [])})
    emit({'type':'assistant','session_id':'fixture','message':{'model':MODEL,'content':[
          {'type':'tool_use','name':'StructuredOutput','input':WIRE}]}})
    emit({'type':'result','session_id':'fixture','structured_output':WIRE,'is_error':False})
elif ADAPTER == 'agy':
    hooks=json.loads((Path(os.environ['HOME'])/'.gemini/config/hooks.json').read_text())
    command=hooks['evaluator-gate']['PreToolUse'][0]['hooks'][0]['command']
    gate=Path(shlex.split(command)[-1])
    gate.with_suffix('.jsonl').write_text(json.dumps({'call':{'name':'finish','args':WIRE},'decision':'allow','count_before':0})+'\\n')
    session='00000000-0000-0000-0000-000000000001'
    log=Path(argv[argv.index('--log-file')+1])
    log.write_text('Created conversation '+session+'\\n'); log.chmod(0o600)
    transcript=Path(os.environ['HOME'])/'.gemini/antigravity-cli/brain'/session/'.system_generated/logs/transcript.jsonl'
    transcript.parent.mkdir(parents=True,mode=0o700)
    transcript.write_text('{\"type\":\"USER_INPUT\"}\\n'); transcript.chmod(0o600)
    emit({'event':'init','init':{'model':MODEL,'agent':'ukrainian-eval-reference-only','json_schema':schema}})
    for i,kind in enumerate(('user_input','finish')):
        emit({'event':'step_update','step_update':{'conversation_id':session,'step_index':i,'state':'DONE','step_type':kind}})
    emit({'event':'result','result':{'conversation_id':session,'num_turns':1,'status':'SUCCESS',
          'structured_output':WIRE,'json_schema':schema,'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}})
elif ADAPTER == 'codex':
    Path(argv[argv.index('--output-last-message')+1]).write_text(raw)
    for arg in argv:
        if arg.startswith('mcp_servers.sources.args='):
            config=json.loads(Path(json.loads(arg.split('=',1)[1])[-1]).read_text())
            canonical=json.dumps(config['schemas'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
            Path(config['journal']).write_text(json.dumps({'event':'ready','tools_sha256':hashlib.sha256(canonical).hexdigest(),
                'server_sha256':config['server_sha256']})+'\\n')
    emit({'type':'thread.started','thread_id':'fixture'})
    emit({'type':'turn.started'})
    emit({'type':'item.completed','item':{'type':'agent_message','text':raw}})
    emit({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}})
else:
    emit({'type':'system','subtype':'init','apiKeySource':'login','session_id':'fixture','model':MODEL})
    emit({'type':'assistant','session_id':'fixture','message':{'role':'assistant','content':[{'type':'text','text':raw}]}})
    emit({'type':'result','subtype':'success','is_error':False,'session_id':'fixture','result':raw})
'''
    # Test constants are injected, not a prompt builder's return value.
    value = wire_responses(answers) if wire is None else wire
    header = (f"ADAPTER={adapter!r}\nMODEL={model!r}\nWIRE={value!r}\n"
              f"TOOLS={TOOLS!r}\n"
              f"CAPTURE={str(capture)!r}\nSCHEMA_CAPTURE={str(schema_capture)!r}\n")
    script = script.replace("'ukrainian-eval-reference-only'", repr(native_agy.PROFILE_NAME))
    binary.write_text("#!" + sys.executable + "\n" + header + script)
    binary.chmod(0o700)
    return binary, capture, schema_capture


def config(adapter, model, binary):
    value = {"schema": "zno-nmt.config.v1", "adapter": adapter, "model": model, "effort": "high",
             "timeout_seconds": 15, "max_output_tokens": 8192, "max_tool_calls": 20, "repeats": 1,
             "tools": TOOLS, "corpus_id": "synthetic"}
    if adapter == "codex":
        value.update(codex_bin=str(binary), codex_tool_policy="reference-only", provider=native_codex.CODEX_PROVIDER)
    elif adapter == "agy":
        value.update(agy_bin=str(binary), provider=native_agy.PROVIDER)
    elif adapter == "cursor":
        value.update(cursor_bin=str(binary), provider=native_cursor.CURSOR_PROVIDER)
    else:
        value.update(claude_bin=str(binary))
    return value


def mock_probes(monkeypatch, tmp_path, binary):
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    monkeypatch.setattr(adapters, "_mcp_list_tools", lambda *_: (LISTING, "d" * 64))
    monkeypatch.setattr(adapters, "_claude_capabilities", lambda *_a, **_k: (str(binary), "fixture"))
    monkeypatch.setattr(native_agy, "_credential", lambda *_: b"synthetic-test-only")
    monkeypatch.setattr(native_agy, "_binary", lambda *_: (str(binary), binary_hash))
    monkeypatch.setattr(native_cursor, "_assert_login", lambda *_: None)
    monkeypatch.setattr(native_cursor, "_probe_cli", lambda *_: native_cursor._CliProbe(str(binary), binary, binary_hash, "fixture"))
    monkeypatch.setattr(native_codex, "_sanitized_chatgpt_auth", lambda *_: b'{"auth_mode":"chatgpt"}')
    monkeypatch.setattr(codex_reference, "bridge_command", lambda: binary)
    def prepare(value, condition, *_args):
        schemas = adapters._reference_catalog(LISTING, value["tools"])
        receipt = {"schemas": schemas, "source_server_sha256": "d" * 64}
        return value, tmp_path, _probe(str(binary)), {}, {}, receipt
    monkeypatch.setattr(codex_reference, "prepare", prepare)


def assert_submission(raw, recorded, adapter, condition):
    expected = recorded.encode()
    if adapter == "agy":
        # Literal independently specified frame; any additional byte fails.
        expected = ('{"event":"user","message":{"content":' + json.dumps(recorded, ensure_ascii=False) + '}}\n').encode()
        assert json.loads(raw)["message"]["content"].encode() == recorded.encode()
    assert raw == expected, f"unexpected task/scaffolding bytes in {adapter}/{condition}"


@pytest.mark.parametrize("condition", ["closed-book", "sources"])
@pytest.mark.parametrize("smoke_intent", [False, True], ids=["study", "smoke"])
def test_six_real_dispatch_paths_capture_identical_task_bytes(branch, condition, smoke_intent, tmp_path, monkeypatch):
    packet, _key, answers = branch
    reference = None
    for index, (adapter, model) in enumerate(ROUTES):
        root = tmp_path / str(index)
        root.mkdir()
        binary, capture, schema_capture = fake_binary(root, adapter, model, answers)
        mock_probes(monkeypatch, root, binary)
        if condition == "closed-book":
            def forbidden(*_args, **_kwargs):
                pytest.fail("closed-book contacted Sources")
            monkeypatch.setattr(adapters, "_mcp_list_tools", forbidden)
            monkeypatch.setattr(adapters, "_mcp_call", forbidden)
        store = EvidenceStore(root / "evidence")
        attempt = store.start({"denominator": 1})
        result = runner.run_exam(packet, config(adapter, model, binary), condition,
            sources_url="https://reference.invalid/mcp" if condition == "sources" else None,
            evidence=attempt.append, smoke_intent=smoke_intent)
        attempt.finalize(result)
        assert result["status"] == "ok", result
        assert result["responses"] == answers
        events = [json.loads(line) for line in (root / "evidence/attempts" / attempt.id / "events.jsonl").read_text().splitlines()]
        recorded = next(item["payload"] for item in events if item["kind"] == "prompt")
        assert_submission(capture.read_bytes(), recorded["text"], adapter, condition)
        assert recorded["sha256"] == hashlib.sha256(recorded["text"].encode()).hexdigest()
        assert recorded["smoke_intent"] is smoke_intent
        trial_input = next(item["payload"] for item in events if item["kind"] == "trial_input")
        assert trial_input["smoke_intent"] is smoke_intent
        assert result["comparison"] == runner._comparison(packet, config(adapter, model, binary),
                                                         smoke_intent=smoke_intent)
        assert ("Exercise every tool" in recorded["text"]) == (smoke_intent and condition == "sources")
        sources_config = json.loads(capture.with_suffix('.sources.json').read_text())
        assert bool(sources_config) == (condition == "sources")
        if reference is None:
            reference = recorded
        assert recorded == reference
        assert recorded["response_schema"] == adapters.response_schema(packet)
        if schema_capture.exists():
            assert json.loads(schema_capture.read_text()) == recorded["response_schema"]
        if condition == "sources":
            catalog = adapters._reference_catalog(LISTING, TOOLS)
            assert adapters.canonical(catalog) in recorded["text"]
            assert "not_configured" not in recorded["text"]
            if smoke_intent:
                assert "SMOKE tool checklist: " + ", ".join(TOOLS) + "." in recorded["text"]
        if adapter == "agy":
            scaffolding = next(item["payload"] for item in events if item["kind"] == "runtime_scaffolding")
            assert scaffolding["sha256"] == hashlib.sha256((native_agy.INPUT_FRAME_PREFIX + native_agy.INPUT_FRAME_SUFFIX).encode()).hexdigest()
            assert any(item["kind"] == "agy_hook_receipts_raw" for item in events)


@pytest.mark.parametrize("condition", ["closed-book", "sources"])
def test_omitted_smoke_intent_matches_false_and_preserves_study_policy(branch, condition, tmp_path, monkeypatch):
    packet, _key, answers = branch
    value = config("claude", ROUTES[4][1], tmp_path / "unused")
    catalog = adapters._reference_catalog(LISTING, TOOLS) if condition == "sources" else []
    kwargs = {"max_tool_calls": 20, "reference_catalog": catalog}
    default = adapters.build_prompt(packet, condition, **kwargs)
    assert default == adapters.build_prompt(packet, condition, smoke_intent=False, **kwargs)
    assert "Exercise every tool" not in default and "SMOKE tool checklist" not in default
    if condition == "sources":
        assert "without any reference-tool call fails" in default
        assert "MUST call verify_stress on every listed option word" in default
        assert "reference dispatcher" in default
    monkeypatch.setattr(runner, "preflight", lambda *_: {
        "reference_catalog": catalog, "tool_schema_sha256": adapters.digest(catalog),
        "mcp_server_identity_sha256": None})
    monkeypatch.setattr(adapters, "run_claude", lambda *_a, **_k: {
        "responses": answers, "identity": {}, "metrics": {}})
    events = []
    explicit_events = []
    result = runner.run_exam(packet, value, condition, evidence=lambda *event: events.append(event))
    explicit = runner.run_exam(packet, value, condition, smoke_intent=False,
                               evidence=lambda *event: explicit_events.append(event))
    assert result == explicit and result["status"] == "ok"
    assert events == explicit_events
    assert result["comparison"] == runner._comparison(packet, value, smoke_intent=False)
    assert result["comparison"] != runner._comparison(packet, value, smoke_intent=True)


@pytest.mark.parametrize("smoke_intent", [False, True])
@pytest.mark.parametrize("condition", ["closed-book", "sources"])
def test_preflight_failure_evidence_binds_smoke_intent(branch, condition, smoke_intent, tmp_path, monkeypatch):
    packet, _key, _answers = branch
    value = config("claude", ROUTES[4][1], tmp_path / "unused")
    def fail(*_args):
        raise adapters.AdapterError("synthetic preflight failure")
    monkeypatch.setattr(runner, "preflight", fail)
    events = []
    result = runner.run_exam(packet, value, condition, smoke_intent=smoke_intent,
                            evidence=lambda *event: events.append(event))
    assert result["status"] == "failed"
    assert [kind for kind, _ in events] == ["trial_input", "trial_failure"]
    assert events[0][1]["smoke_intent"] is smoke_intent
    assert events[1][1] == result
    assert result["comparison"] == runner._comparison(packet, value, smoke_intent=smoke_intent)
    assert result["comparison"] != runner._comparison(packet, value, smoke_intent=not smoke_intent)


@pytest.mark.parametrize("bad_mode", [None, 0, 1, "smoke", []])
def test_non_boolean_smoke_intent_is_rejected_before_preflight(branch, bad_mode, tmp_path, monkeypatch):
    packet, _key, _answers = branch
    value = config("claude", ROUTES[4][1], tmp_path / "unused")
    with pytest.raises(adapters.AdapterError, match="boolean"):
        adapters.build_prompt(packet, "closed-book", smoke_intent=bad_mode)
    def forbidden(*_args):
        pytest.fail("invalid intent reached preflight")
    monkeypatch.setattr(runner, "preflight", forbidden)
    with pytest.raises(runner.ExamError, match="boolean"):
        runner.run_exam(packet, value, "closed-book", smoke_intent=bad_mode)


@pytest.mark.parametrize("catalog", [None, []])
def test_sources_smoke_requires_frozen_catalog(branch, catalog):
    packet, _key, _answers = branch
    with pytest.raises(adapters.AdapterError, match="frozen reference catalog"):
        adapters.build_prompt(packet, "sources", max_tool_calls=20, smoke_intent=True, reference_catalog=catalog)


@pytest.mark.parametrize("smoke_intent", [False, True])
def test_invalid_condition_is_rejected_before_preflight(branch, smoke_intent, tmp_path, monkeypatch):
    packet, _key, _answers = branch
    value = config("claude", ROUTES[4][1], tmp_path / "unused")
    def forbidden(*_args):
        pytest.fail("invalid condition reached preflight")
    monkeypatch.setattr(runner, "preflight", forbidden)
    with pytest.raises(runner.ExamError, match="condition must"):
        runner.run_exam(packet, value, "invalid", smoke_intent=smoke_intent)


@pytest.mark.parametrize("smoke_intent", [False, True])
def test_catalog_drift_fails_before_native_submission_in_both_modes(branch, smoke_intent, tmp_path, monkeypatch):
    packet, _key, _answers = branch
    value = config("claude", ROUTES[4][1], tmp_path / "unused")
    monkeypatch.setattr(runner, "preflight", lambda *_: {
        "reference_catalog": adapters._reference_catalog(LISTING, TOOLS),
        "tool_schema_sha256": "0" * 64, "mcp_server_identity_sha256": None})
    def forbidden(*_args, **_kwargs):
        pytest.fail("catalog drift reached candidate submission")
    monkeypatch.setattr(adapters, "run_claude", forbidden)
    events = []
    result = runner.run_exam(packet, value, "sources", smoke_intent=smoke_intent,
                            evidence=lambda *event: events.append(event))
    assert result["status"] == "failed"
    assert [kind for kind, _ in events] == ["trial_input", "preflight", "trial_failure"]
    assert events[0][1]["smoke_intent"] is smoke_intent
    assert events[-1][1] == result
    assert result["comparison"] == runner._comparison(packet, value, smoke_intent=smoke_intent)


@pytest.mark.parametrize("smoke_intent", [False, True])
@pytest.mark.parametrize("outcome", ["ok", "failed"])
def test_shared_runner_budget_receipt_preserves_mode_on_success_and_failure(
        branch, smoke_intent, outcome, monkeypatch):
    # Existing compatibility callers also use this shared runner; no HTTP is sent.
    from test_zno_nmt_runner import _config
    packet, _key, answers = branch
    value = _config()
    class Budget:
        def __init__(self):
            self.statuses = []
        def finalize(self, status):
            self.statuses.append(status)
            return {"status": status}
    budget = Budget()
    monkeypatch.setattr(runner, "preflight", lambda *_: {
        "tool_schema_sha256": adapters.digest([]), "mcp_server_identity_sha256": None})
    def run(*_args, prompt, request_budget, **_kwargs):
        assert request_budget is budget
        assert prompt == adapters.build_prompt(packet, "closed-book", max_tool_calls=2, reference_catalog=[],
                                              smoke_intent=smoke_intent)
        if outcome == "failed":
            raise adapters.AdapterError("synthetic execution failure")
        return {"responses": answers, "identity": {}, "metrics": {}}
    monkeypatch.setattr(adapters, "run_chat_http", run)
    events = []
    result = runner.run_exam(packet, value, "closed-book", request_budget=budget, smoke_intent=smoke_intent,
                            evidence=lambda *event: events.append(event))
    status = "completed" if outcome == "ok" else "failed"
    assert budget.statuses == [status]
    assert result["status"] == outcome
    assert result["identity"]["request_budget_receipt_sha256"] == adapters.digest({"status": status})
    assert result["comparison"] == runner._comparison(packet, value, smoke_intent=smoke_intent)
    assert result["comparison"] != runner._comparison(packet, value, smoke_intent=not smoke_intent)
    assert dict(events)["trial_input"]["smoke_intent"] is smoke_intent
    if outcome == "failed":
        assert dict(events)["trial_failure"] == result


def test_injected_agy_only_task_suffix_fails_actual_capture_equality(tmp_path, monkeypatch):
    from test_zno_nmt_core import _prepared
    packet, _key = _prepared()
    answers = {"q0001": "A", "q0002": {"r1": "A", "r2": "B"}, "q0003": "A"}
    binary, capture, _schema = fake_binary(tmp_path, "agy", ROUTES[2][1], answers)
    mock_probes(monkeypatch, tmp_path, binary)
    original = native_agy.run_agy
    def injected(*args, prompt, **kwargs):
        return original(*args, prompt=prompt + "\nNo filesystem schema lookup is needed.", **kwargs)
    monkeypatch.setattr(native_agy, "run_agy", injected)
    events = []
    result = runner.run_exam(packet, config("agy", ROUTES[2][1], binary), "sources",
        sources_url="https://reference.invalid/mcp", evidence=lambda *event: events.append(event))
    assert result["status"] == "ok"  # transport success does not prove equality
    recorded = dict(events)["prompt"]["text"]
    with pytest.raises(AssertionError):
        assert_submission(capture.read_bytes(), recorded, "agy", "sources")


@pytest.mark.parametrize("adapter,model", ROUTES)
@pytest.mark.parametrize("condition", ["closed-book", "sources"])
@pytest.mark.parametrize("smoke_intent", [False, True], ids=["study", "smoke"])
def test_failed_native_attempt_keeps_raw_explanation_and_never_salvages_answer(
        branch, adapter, model, condition, smoke_intent, tmp_path, monkeypatch):
    packet, _key, answers = branch
    wire = wire_responses(answers)
    for envelope in wire["responses"].values():
        envelope["answer"] = "invalid\nanswer" if packet["schema"] == adapters.GEC_PACKET_SCHEMA else "invalid-option"
        envelope["explanation"] = "  Keep this raw explanation.\nThe right answer is A; use another correction.  "
    binary, _capture, _schema = fake_binary(tmp_path, adapter, model, answers, wire=wire)
    mock_probes(monkeypatch, tmp_path, binary)
    store = EvidenceStore(tmp_path / "failed-evidence")
    attempt = store.start({"denominator": len(answers)})
    result = runner.run_exam(packet, config(adapter, model, binary), condition,
                            sources_url="https://reference.invalid/mcp" if condition == "sources" else None,
                            evidence=attempt.append, smoke_intent=smoke_intent)
    receipt = attempt.finalize(result, status="failed")
    assert result["status"] == "failed"
    assert result["responses"] == {item_id: None for item_id in answers}
    assert receipt["terminal_status"] == "failed"
    events = [json.loads(line) for line in (tmp_path / "failed-evidence/attempts" / attempt.id / "events.jsonl").read_text().splitlines()]
    for kind in ("trial_input", "prompt"):
        assert next(event["payload"] for event in events if event["kind"] == kind)["smoke_intent"] is smoke_intent
    expected = runner._comparison(packet, config(adapter, model, binary), smoke_intent=smoke_intent)
    assert result["comparison"] == expected
    assert next(event["payload"] for event in events if event["kind"] == "trial_failure")["comparison"] == expected
    assert expected != runner._comparison(packet, config(adapter, model, binary), smoke_intent=not smoke_intent)
    raw = next(event["payload"]["stdout"] for event in events if event["kind"] == "cli_result")
    native_events = [json.loads(line) for line in raw.splitlines()]
    if adapter == "codex":
        answer_text = next(item["item"]["text"] for item in native_events if item["type"] == "item.completed")
        assert json.loads(answer_text) == wire
        final = next(event["payload"]["text"] for event in events if event["kind"] == "cli_final_message")
        assert final == answer_text
    elif adapter == "cursor":
        assert json.loads(native_events[-1]["result"]) == wire
    elif adapter == "claude":
        assert native_events[-1]["structured_output"] == wire
    else:
        assert native_events[-1]["result"]["structured_output"] == wire
    if adapter == "agy":
        hook_raw = next(event["payload"]["text"] for event in events if event["kind"] == "agy_hook_receipts_raw")
        hook = json.loads(hook_raw)
        assert hook["call"]["args"] == wire
    if adapter in {"claude", "agy"}:
        assert result["failure_reason"] != "candidate_response_error"
