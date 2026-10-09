"""Synthetic real-reader regressions for physical JSON framing (#72).

These authored cases are compatibility checks, not independent native proof.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import test_answer_first_contract as contract
from test_codex_reference import _native_fixture
from test_native_agy import config as agy_config, provision
from test_native_validity import capture_fixture

from ukrainian_llm_eval import adapters, codex_reference, core, gec_scoring, mcp_proxy
from ukrainian_llm_eval import native_agy, native_codex, native_cursor

branch = contract.branch
ROUTES = ["sol", "luna", "flash", "grok", "sonnet", "opus"]
UNICODE_BREAKS = ["\u0085", "\u2028", "\u2029"]
FRAMES = [("\n", True), ("\r\n", True), ("\r", True), ("\n", False)]


def serialize(rows, separator="\n", final=True, escaped=False):
    return separator.join(json.dumps(row, ensure_ascii=escaped) for row in rows) + (separator if final else "")


def streams(route, packet, wire, reference="reference"):
    """Produce independently specified native envelopes, with no native calls."""
    raw = json.dumps(wire, ensure_ascii=False)
    if route in {"sol", "luna"}:
        return [
            {"type": "thread.started", "thread_id": "synthetic"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": raw}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
        ]
    if route in {"sonnet", "opus"}:
        model = "claude-sonnet-5-5" if route == "sonnet" else "claude-opus-5-5"
        selector = model + "[1m]"
        return [
            {"type": "system", "subtype": "init", "model": selector,
             "tools": ["StructuredOutput"], "session_id": "synthetic"},
            {"type": "assistant", "session_id": "synthetic", "message": {"model": model, "content": [
                {"type": "tool_use", "name": "StructuredOutput", "input": wire}]}},
            {"type": "result", "session_id": "synthetic", "structured_output": wire,
             "result": raw, "is_error": False,
             "modelUsage": {selector: {"canonicalModel": model, "contextWindow": 1_000_000}}},
        ]
    if route == "grok":
        return [
            {"type": "system", "subtype": "init", "model": "grok-4.7",
             "apiKeySource": "login", "session_id": "synthetic"},
            {"type": "assistant", "session_id": "synthetic", "message": {
                "role": "assistant", "content": [{"type": "text", "text": raw}]}},
            {"type": "result", "subtype": "success", "is_error": False,
             "session_id": "synthetic", "result": raw},
        ]
    schema = adapters.response_schema(packet)
    return [
        {"event": "init", "init": {"model": "gemini-3.8-flash-high",
         "agent": native_agy.PROFILE_NAME, "json_schema": schema}},
        *[{"event": "step_update", "step_update": {"conversation_id": "synthetic", "step_index": index,
           "state": "DONE", "step_type": kind, "text": reference}}
          for index, kind in enumerate(["user_input", "agent_response", "finish"])],
        {"event": "result", "result": {"conversation_id": "synthetic", "num_turns": 1,
         "status": "SUCCESS", "structured_output": wire, "json_schema": schema,
         "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}},
    ]


def read_stream(route, raw, packet, wire):
    if route in {"sol", "luna"}:
        parsed, count = codex_reference.parse_events(raw, packet, [], [])
        assert count == 0 and parsed.answer_failure_reason is None
        assert parsed.session_id == "synthetic"
        return parsed.responses
    if route in {"sonnet", "opus"}:
        assert adapters._has_model_output(raw)
        assert adapters._claude_content_calls(raw) == []
        assert adapters._claude_session_identity(raw) == "synthetic"
        parsed = adapters._parse_stream_json(raw, packet, set(), 0)
        assert parsed[1] == ("claude-sonnet-5-5" if route == "sonnet" else "claude-opus-5-5")
        return parsed[0]
    if route == "grok":
        parsed = native_cursor._parse_stream_envelope(raw, packet, set(), 0)
        assert parsed.session_id == "synthetic" and parsed.answer_failure_reason is None
        assert native_cursor._content_calls(raw) == []
        return parsed.responses
    hooks = [{"decision": "allow", "call": {"name": "finish", "args": wire}}]
    return native_agy.parse_events(raw, packet, agy_config(model="gemini-3.8-flash-high", effort="high"), hooks, [])["responses"]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("character", UNICODE_BREAKS)
@pytest.mark.parametrize("escaped", [False, True], ids=["literal", "escaped"])
@pytest.mark.parametrize("separator,final", FRAMES, ids=["LF", "CRLF", "CR", "no-final-LF"])
def test_all_six_readers_preserve_explanation_and_answer_only_score(branch, route, character, escaped, separator, final):
    packet, key, answers = branch
    explanation = "  before" + character + "after  "
    wire = {"responses": {ident: {"answer": answer, "explanation": explanation} for ident, answer in answers.items()}}
    raw = serialize(streams(route, packet, wire), separator, final, escaped)
    assert read_stream(route, raw, packet, wire) == answers
    flat, explanations, order = adapters._extract_enveloped_responses(json.dumps(wire, ensure_ascii=escaped), packet)
    assert explanations == dict.fromkeys(answers, explanation)
    assert order == dict.fromkeys(answers, ["answer", "explanation"])
    ordinary = {"responses": {ident: {"answer": answer, "explanation": "ordinary"} for ident, answer in answers.items()}}
    ordinary_answers = adapters._extract_enveloped_responses(json.dumps(ordinary), packet)[0]
    if packet["schema"] == adapters.GEC_PACKET_SCHEMA:
        def score_input(value):
            return gec_scoring.scoring_inputs(packet, key, {"schema": "ua-gec.run.v1", "status": "ok",
                "packet_sha256": packet["packet_sha256"], "responses": value})
        assert score_input(flat) == score_input(ordinary_answers)
        assert score_input(flat)[0].encode() == (answers["q0001"] + "\n").encode()
    else:
        def score(value):
            return core.canonical(core.score_run(packet, key, contract._run(packet, condition="closed-book", responses=value)))
        assert score(flat) == score(ordinary_answers)


@pytest.mark.parametrize("separator,final", FRAMES)
@pytest.mark.parametrize("escaped", [False, True])
def test_native_tool_arguments_and_results_keep_all_unicode_breaks(separator, final, escaped):
    value = "before" + "".join(UNICODE_BREAKS) + "after"
    arguments = {"word": value}
    result = {"content": [{"type": "text", "text": value}]}
    claude = [{"type": "tool_use", "id": "call", "name": "mcp__sources__verify_word", "input": arguments},
              {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call", "content": result}]}}]
    assert adapters._claude_content_calls(serialize(claude, separator, final, escaped)) == [
        {"name": "verify_word", "arguments": arguments, "output": result}]
    cursor = [{"type": "tool_call", "subtype": "completed", "tool_call": {"mcpToolCall": {
        "args": {"name": "sources-verify_word", "args": arguments}, "result": {"success": result}}}}]
    assert native_cursor._content_calls(serialize(cursor, separator, final, escaped)) == [
        {"name": "verify_word", "arguments": arguments, "output": result["content"]}]
    adapters._attest_content_calls(adapters._claude_content_calls(serialize(claude, separator, final, escaped)),
                                  [{"name": "verify_word", "arguments": arguments, "result": result}])
    bad = copy.deepcopy(claude)
    bad[-1]["message"]["content"][0]["tool_use_id"] = "different"
    with pytest.raises(adapters.AdapterError):
        adapters._claude_content_calls(serialize(bad, separator, final, escaped))


@pytest.mark.parametrize("separator,final", FRAMES)
@pytest.mark.parametrize("escaped", [False, True])
def test_sse_and_plain_json_preserve_unicode_without_changing_selection(separator, final, escaped):
    value = "before" + "".join(UNICODE_BREAKS) + "after"
    first = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": value}]}}
    second = {"jsonrpc": "2.0", "id": 2, "result": {"text": "second"}}
    line = json.dumps(first, ensure_ascii=escaped)
    sse = ("event: message" + separator + "data: " + line + (separator if final else "")).encode()
    assert adapters._parse_sse_or_json(sse) == mcp_proxy.decode_response(sse) == first
    assert adapters._parse_sse_or_json(line.encode()) == mcp_proxy.decode_response(line.encode()) == first
    # Existing adapter selects the last data record; proxy selects the first result event.
    multiple = ("data: " + line + "\n\ndata: " + json.dumps(second) + "\n\n").encode()
    assert adapters._parse_sse_or_json(multiple) == second
    assert mcp_proxy.decode_response(multiple) == first


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("bad", ["blank", "malformed", "duplicate", "control", "trailing", "incomplete"])
def test_native_invalid_record_and_terminal_compatibility(route, bad):
    packet = {"items": [{"id": "q", "kind": "single"}]}
    wire = {"responses": {"q": {"answer": "A", "explanation": "before\u2028after"}}}
    rows = streams(route, packet, wire)
    raw = serialize(rows)
    if bad == "blank":
        raw = raw.replace("\n", "\n\n", 1)
        if route == "grok":
            assert native_cursor._parse_stream_envelope(raw, packet, set(), 0).responses == {"q": "A"}
            with pytest.raises(adapters.AdapterError):
                native_cursor._content_calls(raw)
            return
        if route == "flash":
            assert read_stream(route, raw, packet, wire) == {"q": "A"}
            return
    elif bad == "malformed":
        raw += "broken\n"
    elif bad == "duplicate":
        raw = raw.replace('{"type":', '{"type":"forged","type":', 1) if route != "flash" else raw.replace(
            '{"event":', '{"event":"forged","event":', 1)
    elif bad == "control":
        raw = raw.replace("before\u2028after", "before\x0bafter")
    elif bad == "trailing":
        raw += "{} {}\n"
    else:
        raw = serialize(rows[:-1])
    with pytest.raises(adapters.AdapterError):
        if route in {"sonnet", "opus"}:
            adapters._parse_stream_json(raw, packet, set(), 0)
        else:
            read_stream(route, raw, packet, wire)


@pytest.mark.parametrize("bad", ['{"result":{},"result":{}}', '{"result":{"text":"before\x0cafter"}}',
                                '{"result":{}} trailing', "{} {}"])
def test_sse_does_not_repair_invalid_json(bad):
    raw = ("data: " + bad + "\n\n").encode()
    with pytest.raises(adapters.AdapterError):
        adapters._parse_sse_or_json(raw)
    with pytest.raises(ValueError):
        mcp_proxy.decode_response(raw)


@pytest.mark.parametrize("character", UNICODE_BREAKS)
def test_gec_prediction_still_requires_one_line(character):
    packet = {"schema": adapters.GEC_PACKET_SCHEMA, "items": [{"id": "q"}]}
    wire = {"responses": {"q": {"answer": "ordinary one-line prediction", "explanation": "before" + character + "after"}}}
    assert adapters._extract_enveloped_responses(wire, packet)[0] == {"q": "ordinary one-line prediction"}
    wire["responses"]["q"]["answer"] = "before" + character + "after"
    with pytest.raises(adapters.AdapterError, match="GEC response"):
        adapters._extract_enveloped_responses(wire, packet)


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"])
def test_controller_journal_complete_line_and_raw_custody(separator):
    text = "before" + "".join(UNICODE_BREAKS) + "after"
    row = {"request": {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "verify_word", "arguments": {"word": text}}},
           "response": {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}}
    catalog = [{"name": "verify_word", "inputSchema": {"type": "object"}}]
    records = []
    with adapters._native_attempt() as attempt:
        raw = serialize([row, row], separator, False).encode() + b"\n"
        path = attempt["root"] / "reference-journal.jsonl"
        path.write_bytes(raw)
        path.chmod(0o600)
        calls, count = adapters._controller_evidence(attempt["root"], catalog, lambda *record: records.append(record))
        assert count == 0 and len(calls) == 2 and calls[0]["arguments"] == {"word": text}
        assert calls[0]["result"] == row["response"]["result"]
        assert base64.b64decode(records[0][1]["raw_base64"]) == raw
        assert records[0][1]["sha256"] == hashlib.sha256(raw).hexdigest()
        for invalid in [serialize([row], separator, False).encode(), raw + b"\n", raw.replace(b'"id": 1', b'"id": 2', 1)]:
            path.write_bytes(invalid)
            with pytest.raises(adapters.AdapterError):
                adapters._controller_evidence(attempt["root"], catalog, None)


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"])
def test_agy_transcript_unicode_keeps_complete_raw_custody(separator):
    records = []
    with adapters._native_attempt() as attempt:
        root, log, app_data, session = capture_fixture(attempt)
        path = app_data / "brain" / session / ".system_generated/logs/transcript.jsonl"
        raw = serialize([{"type": "USER_INPUT", "content": "before" + "".join(UNICODE_BREAKS) + "after"},
                         {"type": "USER_INPUT", "content": "second"}], separator, False).encode() + b"\n"
        path.write_bytes(raw)
        assert native_agy.capture_native_artifacts(root, log, app_data, lambda *record: records.append(record)) == session
        artifact = next(payload for kind, payload in records if kind == "agy_native_artifact" and payload["kind"] == "transcript")
        assert base64.b64decode(artifact["raw_base64"]) == raw
        assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()
        for invalid in [raw.removesuffix(b"\n"), raw + b"\n", b'{"type":"USER_INPUT","type":"GENERIC"}\n']:
            path.write_bytes(invalid)
            with pytest.raises(adapters.AdapterError):
                native_agy.capture_native_artifacts(root, log, app_data, None)


def test_agy_hook_receipt_unicode_through_runner(tmp_path, monkeypatch):
    staging = provision(tmp_path)
    binary = tmp_path / "synthetic-binary"
    binary.write_bytes(b"synthetic")
    monkeypatch.setattr(native_agy, "_binary", lambda _: (str(binary), hashlib.sha256(binary.read_bytes()).hexdigest()))
    packet = {"items": [{"id": "q", "kind": "single"}]}
    wire = {"responses": {"q": {"answer": "A", "explanation": "before" + "".join(UNICODE_BREAKS) + "after"}}}
    records = []
    def process(argv, *, cwd, env, **kwargs):
        hooks = json.loads((Path(env["HOME"]) / ".gemini/config/hooks.json").read_text())
        gate = Path(shlex.split(hooks["evaluator-gate"]["PreToolUse"][0]["hooks"][0]["command"])[-1])
        receipts = [{"decision": "allow", "call": {"name": "finish", "args": wire}}]
        gate.with_suffix(".jsonl").write_bytes(serialize(receipts, "\r\n").encode())
        from native_validity_fixtures import emit_agy_capture
        session = emit_agy_capture(cwd.parent)
        rows = streams("flash", packet, wire)
        for event in rows:
            if "step_update" in event:
                event["step_update"]["conversation_id"] = session
            if "result" in event:
                event["result"]["conversation_id"] = session
        return subprocess.CompletedProcess(argv, 0, serialize(rows), "")
    monkeypatch.setattr(adapters, "_run_claude_process", process)
    trial = native_agy.run_agy(packet, agy_config(model="gemini-3.8-flash-high", effort="high"), "closed-book",
        sources_url=None, prompt="synthetic", private_env_path=staging, evidence=lambda *record: records.append(record))
    assert trial["responses"] == {"q": "A"}
    raw = next(payload["text"] for kind, payload in records if kind == "agy_hook_receipts_raw")
    assert "".join(UNICODE_BREAKS) in raw
    parsed = next(payload for kind, payload in records if kind == "agy_hook_receipts")
    assert parsed[0]["call"]["args"] == wire


def test_codex_journal_unicode_through_runner(tmp_path, monkeypatch):
    config, staging, receipt = _native_fixture(tmp_path, monkeypatch)
    packet = {"items": [{"id": "q", "kind": "single"}]}
    wire = {"responses": {"q": {"answer": "A", "explanation": "before" + "".join(UNICODE_BREAKS) + "after"}}}
    records = []
    def process(argv, **kwargs):
        home = Path(kwargs["env"]["HOME"])
        journal = [{"event": "ready", "tools_sha256": adapters.digest(receipt["schemas"]),
                    "server_sha256": "c" * 64, "note": "before" + "".join(UNICODE_BREAKS) + "after"}]
        path = home / "reference-journal.jsonl"
        path.write_bytes(serialize(journal, "\r\n").encode())
        path.chmod(0o600)
        (home / "final-message.txt").write_text(json.dumps(wire, ensure_ascii=False))
        return subprocess.CompletedProcess(argv, 0, serialize(streams("sol", packet, wire)), "")
    monkeypatch.setattr(native_codex, "_run_process", process)
    trial = codex_reference.run(packet, config, "sources", sources_url="https://reference.invalid/mcp",
        prompt="synthetic", private_env_path=staging, evidence=lambda *record: records.append(record))
    assert trial["responses"] == {"q": "A"}
    raw = next(payload["journal"] for kind, payload in records if kind == "reference_controller")
    assert "".join(UNICODE_BREAKS) in raw


def test_mcp_proxy_remains_standalone_without_package_imports():
    script = "import runpy,sys; m=runpy.run_path(sys.argv[1]); assert m['decode_response'](sys.stdin.buffer.read())['result']['text']=='before\\u2028after'"
    result = subprocess.run([sys.executable, "-I", "-c", script, str(Path(mcp_proxy.__file__).resolve())],
        input='data: {"result":{"text":"before\u2028after"}}\n\n'.encode(), capture_output=True, check=True)
    assert result.stdout == result.stderr == b""


@pytest.mark.parametrize("mutation", ["unsupported", "unstarted", "bootstrap_error", "tool", "missing_usage", "failed"])
def test_codex_physical_stream_keeps_lifecycle_and_policy_refusals(mutation):
    packet = {"items": [{"id": "q", "kind": "single"}]}
    wire = {"responses": {"q": {"answer": "A", "explanation": "before\u2028after"}}}
    rows = streams("sol", packet, wire)
    if mutation == "unsupported":
        rows.insert(2, {"type": "unsupported"})
    elif mutation == "unstarted":
        rows.pop(1)
    elif mutation == "bootstrap_error":
        rows.insert(2, {"type": "item.completed", "item": {"type": "error", "message": "before\u2028after"}})
    elif mutation == "tool":
        rows.insert(2, {"type": "item.completed", "item": {"type": "mcp_call"}})
    elif mutation == "missing_usage":
        del rows[-1]["usage"]["input_tokens"]
    else:
        rows[-1] = {"type": "turn.failed"}
    with pytest.raises(adapters.AdapterError):
        native_codex._parse_events(serialize(rows, "\r\n"), packet)


@pytest.mark.parametrize("mutation", ["tools_type", "tool_limit", "tool_name", "result_error", "misplaced", "disagreement"])
def test_claude_physical_stream_keeps_structured_and_tool_refusals(mutation):
    packet = {"items": [{"id": "q", "kind": "single"}]}
    wire = {"responses": {"q": {"answer": "A", "explanation": "before\u2028after"}}}
    rows = streams("sonnet", packet, wire)
    tools = set()
    if mutation == "tools_type":
        rows[0]["tools"] = "StructuredOutput"
    elif mutation == "tool_limit":
        tools = {"mcp__sources__verify_word"}
        rows[0]["tools"].extend(tools)
        rows.insert(1, {"type": "tool_use", "name": next(iter(tools)), "input": {"word": "before\u2028after"}})
    elif mutation == "tool_name":
        rows[1]["message"]["content"][0]["name"] = None
    elif mutation == "result_error":
        rows[-1]["is_error"] = True
    elif mutation == "misplaced":
        rows[1]["structured_output"] = wire
    else:
        rows[1]["message"]["content"][0]["input"] = copy.deepcopy(wire)
        rows[1]["message"]["content"][0]["input"]["responses"]["q"]["explanation"] = "different"
    with pytest.raises(adapters.AdapterError):
        adapters._parse_stream_json(serialize(rows, "\r\n"), packet, tools, 0)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("escaped", [False, True])
@pytest.mark.parametrize("tamper", [False, True])
def test_unicode_native_content_remains_bound_to_controller(route, escaped, tamper):
    text = "before" + "".join(UNICODE_BREAKS) + "after"
    packet = {"items": [{"id": "q", "kind": "single"}]}
    wire = {"responses": {"q": {"answer": "A", "explanation": text}}}
    arguments = {"word": text}
    result = {"content": [{"type": "text", "text": text}]}
    native_result = {"content": [{"type": "text", "text": "different"}]} if tamper else result
    rows = streams(route, packet, wire)
    controller = [{"name": "verify_word", "arguments": arguments, "result": result}]
    def verify():
        if route in {"sol", "luna"}:
            item = {"type": "mcp_tool_call", "server": "sources", "tool": "verify_word", "id": "call",
                    "arguments": arguments, "status": "in_progress"}
            rows[2:2] = [{"type": "item.started", "item": item},
                         {"type": "item.completed", "item": {**item, "status": "completed", "result": native_result}}]
            journal = [{"event": "call", "index": 1, "tool": "verify_word", "arguments_sha256": adapters.digest(arguments)},
                       {"event": "result", "index": 1, "success": True, "result_sha256": adapters.digest(result)}]
            parsed, count = codex_reference.parse_events(serialize(rows, "\r\n", escaped=escaped), packet, ["verify_word"], journal)
            assert parsed.responses == {"q": "A"} and count == 1
        elif route in {"sonnet", "opus"}:
            rows[0]["tools"].append("mcp__sources__verify_word")
            rows[1:1] = [{"type": "tool_use", "id": "call", "name": "mcp__sources__verify_word", "input": arguments},
                         {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call", "content": native_result}]}}]
            raw = serialize(rows, "\r\n", escaped=escaped)
            assert adapters._parse_stream_json(raw, packet, {"mcp__sources__verify_word"}, 1)[0] == {"q": "A"}
            adapters._attest_content_calls(adapters._claude_content_calls(raw), controller)
        elif route == "grok":
            payload = {"args": {"name": "sources-verify_word", "serverIdentifier": "sources", "args": arguments}}
            rows[1:1] = [{"type": "tool_call", "subtype": "started", "call_id": "call", "session_id": "synthetic",
                         "tool_call": {"mcpToolCall": payload}},
                        {"type": "tool_call", "subtype": "completed", "call_id": "call", "session_id": "synthetic",
                         "tool_call": {"mcpToolCall": {**payload, "result": {"success": native_result}}}}]
            raw = serialize(rows, "\r\n", escaped=escaped)
            parsed = native_cursor._parse_stream_envelope(raw, packet, {"verify_word"}, 1)
            assert parsed.responses == {"q": "A"} and parsed.tool_calls == 1
            adapters._attest_content_calls(native_cursor._content_calls(raw), controller)
        else:
            params = {"ServerName": "sources", "ToolName": "verify_word", "Arguments": arguments}
            rows.insert(2, {"event": "step_update", "step_update": {"conversation_id": "synthetic", "step_index": 3,
                "state": "DONE", "step_type": "tool", "tool_name": "call_mcp_tool",
                "tool_info": {"parameters": params, "output": native_result["content"][0]["text"]}}})
            hooks = [{"decision": "allow", "call": {"name": "call_mcp_tool", "args": params}},
                     {"decision": "allow", "call": {"name": "finish", "args": wire}}]
            parsed = native_agy.parse_events(serialize(rows, "\r\n", escaped=escaped), packet,
                agy_config(model="gemini-3.8-flash-high", effort="high"), hooks, controller)
            assert parsed["responses"] == {"q": "A"} and parsed["metrics"]["tool_calls"] == 1
    if tamper:
        with pytest.raises(adapters.AdapterError):
            verify()
    else:
        verify()


@pytest.mark.parametrize("raw,expected", [("", []), ("\n", [""]), ("\r\n", [""]), ("\r", [""]),
    ("a\r\nb\rc\n", ["a", "b", "c"]), ("a\n\n", ["a", ""]), ("\na", ["", "a"]),
    ("a\u0085\u2028\u2029b", ["a\u0085\u2028\u2029b"])])
def test_physical_lines_empty_and_final_record_compatibility(raw, expected):
    assert mcp_proxy.physical_lines(raw) == expected
