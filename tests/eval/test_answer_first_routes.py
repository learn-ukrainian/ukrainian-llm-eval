"""Actual fake-binary stdin captures across all six study routes (#69).

Only credential/control probes are replaced; real dispatch and wire extraction
run. This proves adapter contracts, not native isolation or provider behavior.
"""
from __future__ import annotations

import hashlib
import json
import sys

import pytest
import test_answer_first_contract as contract
from answer_first_fixtures import wire_responses
from test_native_codex import _probe

from ukrainian_llm_eval import adapters, codex_reference, native_agy, native_codex, native_cursor, runner
from ukrainian_llm_eval.evidence import EvidenceStore

branch = contract.branch

ROUTES = [("codex", "gpt-6.1-sol"), ("codex", "gpt-6-luna"), ("agy", "gemini-3.8-flash-high"),
          ("cursor", "grok-4.7"), ("claude", "claude-sonnet-5-5"), ("claude", "claude-opus-5-5")]
LISTING = [{"name": "verify_stress", "inputSchema": {"type": "object"}, "description": "Stress reference"},
           {"name": "not_configured", "inputSchema": {"type": "object"}},
           {"name": "verify_words", "inputSchema": {"type": "object"}, "description": "Form reference"}]


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
          'tools':['StructuredOutput'] + (['mcp__sources__verify_words','mcp__sources__verify_stress'] if 'TRUSTED REFERENCE CATALOG' in prompt else [])})
    emit({'type':'assistant','session_id':'fixture','message':{'model':MODEL,'content':[
          {'type':'tool_use','name':'StructuredOutput','input':WIRE}]}})
    emit({'type':'result','session_id':'fixture','structured_output':WIRE,'is_error':False})
elif ADAPTER == 'agy':
    hooks=json.loads((Path(os.environ['HOME'])/'.gemini/config/hooks.json').read_text())
    command=hooks['evaluator-gate']['PreToolUse'][0]['hooks'][0]['command']
    gate=Path(shlex.split(command)[-1])
    gate.with_suffix('.jsonl').write_text(json.dumps({'call':{'name':'finish','args':WIRE},'decision':'allow','count_before':0})+'\\n')
    emit({'event':'init','init':{'model':MODEL,'agent':'ukrainian-eval-reference-only','json_schema':schema}})
    for i,kind in enumerate(('user_input','finish')):
        emit({'event':'step_update','step_update':{'conversation_id':'fixture','step_index':i,'state':'DONE','step_type':kind}})
    emit({'event':'result','result':{'conversation_id':'fixture','num_turns':1,'status':'SUCCESS',
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
              f"CAPTURE={str(capture)!r}\nSCHEMA_CAPTURE={str(schema_capture)!r}\n")
    script = script.replace("'ukrainian-eval-reference-only'", repr(native_agy.PROFILE_NAME))
    binary.write_text("#!" + sys.executable + "\n" + header + script)
    binary.chmod(0o700)
    return binary, capture, schema_capture


def config(adapter, model, binary):
    value = {"schema": "zno-nmt.config.v1", "adapter": adapter, "model": model, "effort": "high",
             "timeout_seconds": 15, "max_output_tokens": 8192, "max_tool_calls": 20, "repeats": 1,
             "tools": ["verify_words", "verify_stress"], "corpus_id": "synthetic"}
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
    monkeypatch.setattr(native_cursor, "_assert_no_global_mcp", lambda: None)
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
def test_six_real_dispatch_paths_capture_identical_task_bytes(branch, condition, tmp_path, monkeypatch):
    packet, _key, answers = branch
    reference = None
    for index, (adapter, model) in enumerate(ROUTES):
        root = tmp_path / str(index)
        root.mkdir()
        binary, capture, schema_capture = fake_binary(root, adapter, model, answers)
        mock_probes(monkeypatch, root, binary)
        store = EvidenceStore(root / "evidence")
        attempt = store.start({"denominator": 1})
        result = runner.run_exam(packet, config(adapter, model, binary), condition,
            sources_url="https://reference.invalid/mcp" if condition == "sources" else None, evidence=attempt.append)
        attempt.finalize(result)
        assert result["status"] == "ok", result
        assert result["responses"] == answers
        events = [json.loads(line) for line in (root / "evidence/attempts" / attempt.id / "events.jsonl").read_text().splitlines()]
        recorded = next(item["payload"] for item in events if item["kind"] == "prompt")
        assert_submission(capture.read_bytes(), recorded["text"], adapter, condition)
        assert recorded["sha256"] == hashlib.sha256(recorded["text"].encode()).hexdigest()
        if reference is None:
            reference = recorded
        assert recorded == reference
        assert recorded["response_schema"] == adapters.response_schema(packet)
        if schema_capture.exists():
            assert json.loads(schema_capture.read_text()) == recorded["response_schema"]
        if condition == "sources":
            catalog = adapters._reference_catalog(LISTING, ["verify_words", "verify_stress"])
            assert adapters.canonical(catalog) in recorded["text"]
            assert "not_configured" not in recorded["text"]
        if adapter == "agy":
            scaffolding = next(item["payload"] for item in events if item["kind"] == "runtime_scaffolding")
            assert scaffolding["sha256"] == hashlib.sha256((native_agy.INPUT_FRAME_PREFIX + native_agy.INPUT_FRAME_SUFFIX).encode()).hexdigest()
            assert any(item["kind"] == "agy_hook_receipts_raw" for item in events)


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
def test_failed_native_attempt_keeps_raw_explanation_and_never_salvages_answer(branch, adapter, model, tmp_path, monkeypatch):
    packet, _key, answers = branch
    wire = wire_responses(answers)
    for envelope in wire["responses"].values():
        envelope["answer"] = "invalid\nanswer" if packet["schema"] == adapters.GEC_PACKET_SCHEMA else "invalid-option"
        envelope["explanation"] = "  Keep this raw explanation.\nThe right answer is A; use another correction.  "
    binary, _capture, _schema = fake_binary(tmp_path, adapter, model, answers, wire=wire)
    mock_probes(monkeypatch, tmp_path, binary)
    store = EvidenceStore(tmp_path / "failed-evidence")
    attempt = store.start({"denominator": len(answers)})
    result = runner.run_exam(packet, config(adapter, model, binary), "closed-book", evidence=attempt.append)
    receipt = attempt.finalize(result, status="failed")
    assert result["status"] == "failed"
    assert result["responses"] == {item_id: None for item_id in answers}
    assert receipt["terminal_status"] == "failed"
    events = [json.loads(line) for line in (tmp_path / "failed-evidence/attempts" / attempt.id / "events.jsonl").read_text().splitlines()]
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
