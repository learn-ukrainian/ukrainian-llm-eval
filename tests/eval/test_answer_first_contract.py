"""Non-author acceptance cases for the wire/saved-answer boundary (#69).

These are synthetic credential-free contracts, not native smoke attestations.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path

import pytest
from answer_first_fixtures import wire_responses
from test_gec_scoring import inputs as gec_inputs
from test_native_agy import call_receipts, config as agy_config, events as agy_events, hook_receipts, packet as agy_packet
from test_native_agy import serialize
from test_native_codex import _packet as codex_packet
from test_native_cursor import _packet as cursor_packet, _stream_events
from test_zno_nmt_core import _exam, _prepared, _run

from ukrainian_llm_eval import adapters, core, gec_scoring, native_agy, native_codex, native_cursor
from ukrainian_llm_eval.evidence import EvidenceStore


@pytest.fixture(params=["single", "matching", "gec"])
def branch(request, tmp_path):
    if request.param == "gec":
        packet, key = gec_inputs(tmp_path)
        answer = EvidenceStore(tmp_path / "runs").verify("candidate")["result"]["responses"]
    else:
        exam = _exam()
        exam["items"] = [item for item in exam["items"] if item["kind"] == request.param][:1]
        exam["scoring"].update(expected_items=1, expected_points=2 if request.param == "matching" else 1)
        packet, key = core.prepare_exam(exam)
        answer = {"q0001": {"r1": "A", "r2": "B"} if request.param == "matching" else "A"}
    return packet, key, answer


def test_exact_schema_and_answer_types(branch):
    packet, _key, answers = branch
    value = wire_responses(answers)
    parsed, explanation, order = adapters._extract_enveloped_responses(json.dumps(value), packet)
    assert parsed == adapters._extract_responses({"responses": answers}, packet)
    assert explanation == {item_id: "Fixture evidence." for item_id in answers}
    assert order == {item_id: ["answer", "explanation"] for item_id in answers}
    schema = adapters.response_schema(packet)
    for envelope in schema["properties"]["responses"]["properties"].values():
        assert envelope["required"] == ["answer", "explanation"]
        assert list(envelope["properties"]) == ["answer", "explanation"]
        assert envelope["additionalProperties"] is False
        assert "maxLength" not in json.dumps(envelope)
    assert adapters._extract_enveloped_responses(json.dumps(wire_responses({key: None for key in answers})), packet)[0] == {key: None for key in answers}


@pytest.mark.parametrize("explanation", ["I chose B; correction: a different sentence.", "  citation\nwith spaces  ", "x" * 10000])
def test_explanation_never_changes_score_or_prediction_bytes(branch, explanation):
    packet, key, answers = branch
    before = wire_responses(answers)
    after = copy.deepcopy(before)
    for envelope in after["responses"].values():
        envelope["explanation"] = explanation
    old = adapters._extract_enveloped_responses(json.dumps(before), packet)
    new = adapters._extract_enveloped_responses(json.dumps(after), packet)
    assert old[0] == new[0] == answers
    assert old[1] != new[1]
    assert set(new[1].values()) == {explanation}
    if packet["schema"] == adapters.GEC_PACKET_SCHEMA:
        def score_inputs(responses):
            return gec_scoring.scoring_inputs(packet, key, {"schema": "ua-gec.run.v1", "status": "ok",
                "packet_sha256": packet["packet_sha256"], "responses": responses})
        assert score_inputs(old[0]) == score_inputs(new[0])
        assert score_inputs(new[0])[0].encode() == (answers["q0001"] + "\n").encode()
    else:
        def score(responses):
            return core.canonical(core.score_run(packet, key, _run(packet, condition="closed-book", responses=responses)))
        assert score(old[0]).encode() == score(new[0]).encode()


def test_wrong_answer_is_not_repaired_by_correct_explanation():
    packet, key = _prepared()
    answers = {"q0001": "B", "q0002": {"r1": "C", "r2": "C"}, "q0003": "A"}
    wire = wire_responses(answers)
    wire["responses"]["q0001"]["explanation"] = "The correct answer is A."
    parsed = adapters._extract_enveloped_responses(json.dumps(wire), packet)[0]
    score = core.score_run(packet, key, _run(packet, condition="closed-book", responses=parsed))
    assert parsed == answers
    assert score["raw_points"] == 0


@pytest.mark.parametrize("mutation", ["missing", "blank", "not_string", "extra_field", "extra_id", "missing_id", "order", "bad_answer"])
def test_wire_rejection_does_not_consult_explanation(branch, mutation):
    packet, _key, answers = branch
    value = wire_responses(answers)
    envelope = value["responses"]["q0001"]
    if mutation == "missing":
        del envelope["explanation"]
    elif mutation == "blank":
        envelope["explanation"] = " \t\n"
    elif mutation == "not_string":
        envelope["explanation"] = 1
    elif mutation == "extra_field":
        envelope["source"] = "correct-looking source"
    elif mutation == "extra_id":
        value["responses"]["extra"] = copy.deepcopy(envelope)
    elif mutation == "missing_id":
        del value["responses"]["q0001"]
    elif mutation == "order":
        value["responses"]["q0001"] = {"explanation": envelope["explanation"], "answer": envelope["answer"]}
    else:
        answer = envelope["answer"]
        envelope["answer"] = "two\nlines" if packet["schema"] == adapters.GEC_PACKET_SCHEMA else ({} if isinstance(answer, dict) else "invalid-option")
    raw = json.dumps(value)
    with pytest.raises(adapters.AdapterError):
        adapters._extract_enveloped_responses(raw, packet)


@pytest.mark.parametrize("raw", ['{"responses":{"q0001":{"answer":"A","answer":"B","explanation":"why"}}}',
    '{"responses":{"q0001":{"answer":"A","explanation":"why","explanation":"other"}}}',
    '{"responses":{},"responses":{}}', '{"responses":'])
def test_duplicate_keys_and_malformed_json_rejected(raw):
    packet, _key = _prepared()
    with pytest.raises(adapters.AdapterError):
        adapters._extract_enveloped_responses(raw, packet)


@pytest.mark.parametrize("route", ["claude", "agy", "codex", "cursor"])
def test_raw_presentation_is_not_repaired_by_canonical_sorting(route):
    if route == "claude":
        packet = codex_packet()
        envelope = {"responses": {"opaque-1": {"explanation": "why", "answer": "A"}}}
        events = [{"type": "system", "subtype": "init", "tools": ["StructuredOutput"]},
                  {"type": "tool_use", "name": "StructuredOutput", "input": envelope},
                  {"type": "result", "structured_output": envelope}]
        with pytest.raises(adapters.AdapterError, match="order"):
            adapters._parse_stream_json(serialize(events), packet, set(), 20)
    elif route == "agy":
        events, hooks = agy_events(), hook_receipts()
        envelope = {"responses": {"q1": {"explanation": "why", "answer": "A"}}}
        events[-1]["result"]["structured_output"] = envelope
        hooks[-1]["call"]["args"] = copy.deepcopy(envelope)
        with pytest.raises(adapters.AdapterError, match="order"):
            native_agy.parse_events(serialize(events), agy_packet(), agy_config(), hooks, call_receipts())
    elif route == "codex":
        packet = codex_packet()
        raw = '{"responses":{"opaque-1":{"explanation":"why","answer":"A"}}}'
        events = [{"type": "thread.started", "thread_id": "session"}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": raw}},
                  {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
        parsed = native_codex._parse_events(serialize(events), packet, final_message=raw)
        assert parsed.responses is None and "order" in parsed.answer_failure_reason
        assert parsed.answer_content == raw
    else:
        events = _stream_events()
        raw = '{"responses":{"opaque-1":{"explanation":"why","answer":"A"}}}'
        events[1]["message"]["content"][0]["text"] = raw
        events[-1]["result"] = raw
        parsed = native_cursor._parse_stream_envelope(serialize(events), cursor_packet(), set(), 20)
        assert parsed.responses is None and parsed.answer_content == raw


@pytest.mark.parametrize("route", ["claude", "agy", "cursor"])
@pytest.mark.parametrize("difference", ["explanation", "order"])
def test_dual_native_sources_disagree_fail_closed(route, difference):
    item_id = "q1" if route == "agy" else "opaque-1"
    value = wire_responses({item_id: "A"})
    other = copy.deepcopy(value)
    if difference == "explanation":
        other["responses"][item_id]["explanation"] += " "
    else:
        other["responses"][item_id] = {"explanation": "Fixture evidence.", "answer": "A"}
    if route == "agy":
        events, hooks = agy_events(), hook_receipts()
        hooks[-1]["call"]["args"] = other
        with pytest.raises(adapters.AdapterError):
            native_agy.parse_events(serialize(events), agy_packet(), agy_config(), hooks, [])
    elif route == "claude":
        events = [{"type": "system", "subtype": "init", "tools": ["StructuredOutput"]},
                  {"type": "tool_use", "name": "StructuredOutput", "input": other},
                  {"type": "result", "structured_output": value}]
        with pytest.raises(adapters.AdapterError):
            adapters._parse_stream_json(serialize(events), codex_packet(), set(), 20)
    else:
        events = _stream_events()
        events[1]["message"]["content"][0]["text"] = json.dumps(other)
        events[-1]["result"] = json.dumps(value)
        if difference == "order":
            assert native_cursor._parse_stream_envelope(serialize(events), cursor_packet(), set(), 20).responses is None
        else:
            with pytest.raises(adapters.AdapterError, match="disagree"):
                native_cursor._parse_stream_envelope(serialize(events), cursor_packet(), set(), 20)


def test_saved_flat_validator_is_unchanged_and_historical_consumers_keep_it():
    source = inspect.getsource(adapters._extract_responses)
    assert hashlib.sha256(source.encode()).hexdigest() == "5bd0c82d72272408b676c0d65cb473d7ffaf36d690cbc764c15390d7272d7351"
    for module in ("scheduling.py", "research_scoring.py"):
        source = Path(adapters.__file__).with_name(module).read_text()
        assert '_extract_responses({"responses": result.get("responses")},' in source
    packet, _key = _prepared()
    answers = {"q0001": "A", "q0002": {"r1": "A", "r2": "B"}, "q0003": "A"}
    assert adapters._extract_responses({"responses": answers}, packet) == answers


def test_catalog_order_duplicates_missing_and_size_failures(monkeypatch):
    tools = [{"name": name, "inputSchema": {"type": "object"}, "description": name * 30}
             for name in ("verify_stress", "extra", "verify_words")]
    configured = ["verify_words", "verify_stress"]
    ordered = adapters._reference_catalog(tools, configured)
    assert [item["name"] for item in ordered] == configured
    with pytest.raises(adapters.AdapterError, match="listing"):
        adapters._reference_catalog(None, configured)
    with pytest.raises(adapters.AdapterError, match="duplicated"):
        adapters._reference_catalog(tools + tools[:1], configured)
    with pytest.raises(adapters.AdapterError, match="configured"):
        adapters._reference_catalog(tools[:1], configured)
    packet, _key = _prepared()
    config = {"tools": configured, "timeout_seconds": 15, "adapter": "claude"}
    calls = []
    def listing(*args):
        calls.append(args)
        return tools, "d" * 64
    monkeypatch.setattr(adapters, "_mcp_list_tools", listing)
    assert adapters.prompt_reference_catalog(config, "closed-book", None) == [] and calls == []
    assert adapters.prompt_reference_catalog(config, "sources", "https://reference.invalid") == ordered
    assert len(calls) == 1
    bare = adapters.build_prompt(packet, "sources", max_tool_calls=20)
    full = adapters.build_prompt(packet, "sources", max_tool_calls=20, reference_catalog=ordered)
    assert len(full.encode()) > len(bare.encode()) + len(adapters.canonical(ordered).encode())
    monkeypatch.setattr(adapters, "_mcp_list_tools", lambda *_: ([], None))
    with pytest.raises(adapters.AdapterError, match="configured"):
        adapters.prompt_reference_catalog(config, "sources", "https://reference.invalid")


@pytest.mark.parametrize("condition,limit", [("unknown", 20), ("sources", None), ("sources", True), ("sources", 0)])
def test_prompt_unknown_condition_or_invalid_budget_rejected(condition, limit):
    packet, _key = _prepared()
    with pytest.raises(adapters.AdapterError):
        adapters.build_prompt(packet, condition, max_tool_calls=limit)


def test_cursor_result_narration_is_not_repaired_from_last_valid_assistant():
    events = _stream_events()
    events[-1]["result"] = "Checking Sources" + events[-1]["result"]
    parsed = native_cursor._parse_stream_envelope(serialize(events), cursor_packet(), set(), 20)
    assert parsed.responses is None
    assert parsed.answer_content == events[-1]["result"]


@pytest.mark.parametrize("events", [[], ["tool"], ["tool", "tool"], ["result", "result"]])
def test_claude_missing_duplicate_or_misplaced_order_sources_rejected(events):
    value = wire_responses({"opaque-1": "A"})
    stream = [{"type": "system", "subtype": "init", "tools": ["StructuredOutput"]}]
    for event in events:
        if event == "tool":
            stream.append({"type": "tool_use", "name": "StructuredOutput", "input": value})
        else:
            stream.append({"type": "result", "structured_output": value})
    if events != ["result", "result"]:
        stream.append({"type": "result", "structured_output": value})
    if events == ["tool"]:
        # A structured payload outside a terminal result is not an authority.
        stream[1]["structured_output"] = value
    with pytest.raises(adapters.AdapterError):
        adapters._parse_stream_json(serialize(stream), codex_packet(), set(), 20)
