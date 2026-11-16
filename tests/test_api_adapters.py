"""Offline contract tests for the direct Anthropic and OpenAI API adapters.

Every test uses a fake SDK client; nothing here opens a network connection or
reads a key.  Each test names the defect class it can catch.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from ukrainian_llm_eval import api_anthropic, api_openai, api_suite
from ukrainian_llm_eval.adapters import build_prompt, canonical
from ukrainian_llm_eval.core import prepare_exam, score_run, write_private_json
from ukrainian_llm_eval.evidence import EvidenceStore
from ukrainian_llm_eval.gec import GEC_PACKET_SCHEMA
from ukrainian_llm_eval.runner import run_exam
from ukrainian_llm_eval.segmentation import derive_segment_plan

_EXAM = {
    "schema": "zno-nmt.exam.v1",
    "title": "Synthetic API adapter fixture",
    "subject": "synthetic",
    "year": 2026,
    "provenance": {"source_url": "https://example.org/synthetic", "source_revision": "r1",
                   "license": "synthetic-example", "exposure": "public-exposure-possible"},
    "scoring": {"kind": "benchmark", "policy_url": None, "pass_threshold": None,
                "expected_items": 2, "expected_points": 2},
    "items": [
        {"id": "1", "kind": "single", "question": "Оберіть X.", "rows": [], "correct": "A",
         "options": [{"id": "A", "text": "X"}, {"id": "B", "text": "Y"}]},
        {"id": "2", "kind": "single", "question": "Оберіть Y.", "rows": [], "correct": "B",
         "options": [{"id": "A", "text": "X"}, {"id": "B", "text": "Y"}]},
    ],
}


def _config(adapter: str, model: str, **extra) -> dict:
    return {"schema": "zno-nmt.config.v1", "adapter": adapter, "model": model, "effort": "high",
            "timeout_seconds": 60, "max_output_tokens": 2000, "max_tool_calls": 1, "repeats": 1,
            "tools": [], "corpus_id": None, "provider": None, **extra}


def _packet_and_key():
    return prepare_exam(json.loads(json.dumps(_EXAM)))


def _correct(packet: dict, key: dict) -> dict:
    return {item["id"]: key["answers"][item["id"]] for item in packet["items"]}


def _answers_for(params: dict, answer_by_question: dict) -> dict:
    """Answer each item in a request prompt from its question text (works for segments)."""
    prompt = params["messages" if "messages" in params else "input"][0]["content"]
    packet = json.loads(prompt.rsplit("\n", 1)[1])
    return {item["id"]: answer_by_question[item["question"]] for item in packet["items"]}


_GOLD = {"Оберіть X.": "A", "Оберіть Y.": "B"}


def _claude_message(params: dict, *, model: str | None = None, stop: str = "end_turn", answers=None) -> dict:
    text = json.dumps({"responses": answers if answers is not None else _answers_for(params, _GOLD)})
    return {"id": "msg_" + str(abs(hash(text)) % 10**8), "type": "message", "role": "assistant",
            "model": model or params["model"], "stop_reason": stop,
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": text}],
            "usage": {"input_tokens": 1000, "output_tokens": 500}}


def _openai_response(params: dict, *, model: str | None = None) -> dict:
    text = json.dumps({"responses": _answers_for(params, _GOLD)})
    return {"id": "resp_1", "object": "response", "model": model or params["model"] + "-2026-11-01",
            "status": "completed", "output": [
                {"type": "reasoning", "id": "rs_1", "summary": []},
                {"type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}}


class FakeAnthropic:
    def __init__(self, reply=_claude_message):
        self.created: list[dict] = []
        self.batch_requests: list[dict] = []
        self.status = "in_progress"
        self.batch_outcomes: dict[str, str] = {}
        self.extra_entries: list[dict] = []
        self.extra_lines: list[str] = []

        def create(**kwargs):
            kwargs.pop("timeout")
            self.created.append(kwargs)
            return reply(kwargs)

        def batch_create(requests):
            self.batch_requests = requests
            return {"id": "msgbatch_1", "processing_status": "in_progress"}

        def results(_batch_id):
            entries = []
            for request in reversed(self.batch_requests):  # results are not in request order
                outcome = self.batch_outcomes.get(request["custom_id"], "succeeded")
                result = {"type": outcome}
                if outcome == "succeeded":
                    result["message"] = _claude_message(request["params"])
                entries.append({"custom_id": request["custom_id"], "result": result})
            # Like the SDK's JSONL decoder: the raw lines are on its HTTP response.
            lines = [json.dumps(entry) for entry in entries + self.extra_entries] + self.extra_lines
            return SimpleNamespace(http_response=SimpleNamespace(iter_lines=lambda: iter(lines)))

        self.messages = SimpleNamespace(create=create, batches=SimpleNamespace(
            create=batch_create, retrieve=lambda _id: {"id": _id, "processing_status": self.status},
            results=results))


def _use_client(monkeypatch, module, client) -> None:
    monkeypatch.setattr(module, "make_client", lambda: client)


def _forbid_client(monkeypatch, module) -> None:
    def refuse():
        raise AssertionError("a client must not be created")

    monkeypatch.setattr(module, "make_client", refuse)


def _write(tmp_path: Path, name: str, value) -> Path:
    path = tmp_path / name
    write_private_json(path, value)
    return path


def _last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_request_shapes_fix_settings_and_drop_unsupported_schema_keywords():
    """Defect class: a request that silently omits or alters the configured settings."""
    packet, _key = _packet_and_key()
    prompt = build_prompt(packet, "closed-book", max_tool_calls=1)
    claude = api_anthropic.build_request(packet, _config("anthropic-api", "claude-opus-5-5"), prompt)
    assert claude == {
        "model": "claude-opus-5-5", "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}],
        "output_config": {"effort": "high", "format": {"type": "json_schema",
                                                       "schema": api_suite.api_schema(packet, enum_nulls=True)}},
    }
    answers = claude["output_config"]["format"]["schema"]["properties"]["responses"]["properties"]
    assert all(answer == {"enum": ["A", "B", None]} for answer in answers.values())
    assert "temperature" not in claude and "thinking" not in claude
    tempered = api_anthropic.build_request(packet, _config("anthropic-api", "claude-sonnet-5-5", temperature=0), prompt)
    assert tempered["temperature"] == 0
    gpt = api_openai.build_request(packet, _config("openai-api", "gpt-6.1-sol", temperature=0.5), prompt)
    assert gpt == {
        "model": "gpt-6.1-sol", "input": [{"role": "user", "content": prompt}], "max_output_tokens": 2000,
        "text": {"format": {"type": "json_schema", "name": "zno_nmt_responses",
                            "schema": api_suite.api_schema(packet), "strict": True}},
        "store": False, "reasoning": {"effort": "high"}, "temperature": 0.5,
    }
    gec_schema = canonical(api_suite.api_schema({"schema": GEC_PACKET_SCHEMA, "items": [{"id": "q0001"}]}))
    assert "minLength" not in gec_schema and "pattern" in gec_schema and "anyOf" not in gec_schema
    # Anthropic rejects more than 16 union-typed parameters; refuse while planning, not after submission.
    wide = {"schema": "zno-nmt.questions.v1", "items": [{"id": f"q{index:04d}", "kind": "single", "options": []}
                                                        for index in range(17)]}
    with pytest.raises(ValueError, match="16 union"):
        api_anthropic.build_request(wide, _config("anthropic-api", "claude-opus-5-5"), prompt)
    with pytest.raises(ValueError):
        api_suite.validate_api_config(_config("anthropic-api", "claude-opus-5-5", tools=["verify_word"]))
    with pytest.raises(ValueError):
        api_suite.validate_api_config(_config("anthropic-api", "claude-opus-5-5", temperature=1.5))


def test_sync_suite_records_settings_usage_cost_and_scores_with_existing_scorer(tmp_path, monkeypatch, capsys):
    """Defect class: rows missing model/settings/usage/cost, or runs the existing scorer cannot read."""
    packet, key = _packet_and_key()
    client = FakeAnthropic()
    _use_client(monkeypatch, api_anthropic, client)
    config = _config("anthropic-api", "claude-sonnet-5-5", temperature=0.2, repeats=2)
    args = ["suite", "--config", str(_write(tmp_path, "c.json", config)),
            "--questions", str(_write(tmp_path, "q.json", packet)), "--out-dir", str(tmp_path / "out"),
            "--max-usd", "1"]
    assert api_anthropic.main(args) == 0
    summary = _last_json(capsys)
    assert (summary["status"], summary["requests"], summary["ok"]) == ("ok", 2, 2)
    assert len(client.created) == 2 and client.created[0]["temperature"] == 0.2
    row = json.loads((tmp_path / "out" / "rows" / "r001-all.json").read_text())
    expected_cost = float((Decimal(1000) * 2 + Decimal(500) * 10) / Decimal(1_000_000))
    assert row["settings"] == {"model": "claude-sonnet-5-5", "effort": "high", "temperature": 0.2,
                               "max_output_tokens": 2000, "timeout_seconds": 60}
    assert row["identity"]["effective_model"] == "claude-sonnet-5-5"
    assert (row["metrics"]["input_tokens"], row["metrics"]["output_tokens"]) == (1000, 500)
    assert row["metrics"]["cost_usd"] == pytest.approx(expected_cost)
    run = json.loads((tmp_path / "out" / "runs" / "repeat-002.json").read_text())
    assert run["repeat"] == 2 and run["identity"]["temperature"] == 0.2
    assert score_run(packet, key, run)["raw_points"] == 2
    receipt = EvidenceStore(tmp_path / "out" / "evidence").verify("repeat-001")
    assert receipt["complete"] and receipt["result"]["responses"] == _correct(packet, key)


def test_max_usd_is_required_and_refuses_before_any_client_or_file(tmp_path, monkeypatch, capsys):
    """Defect class: a paid run that starts although its estimate exceeds the cap."""
    packet, _key = _packet_and_key()
    _forbid_client(monkeypatch, api_openai)
    base = ["suite", "--config", str(_write(tmp_path, "c.json", _config("openai-api", "gpt-6.1-sol"))),
            "--questions", str(_write(tmp_path, "q.json", packet)), "--out-dir", str(tmp_path / "out")]
    assert api_openai.main(base + ["--max-usd", "0.001"]) == 2
    refused = _last_json(capsys)
    assert refused["status"] == "refused" and refused["estimated_usd_upper_bound"] > 0.001
    assert api_openai.main(base) == 2
    assert "max-usd" in _last_json(capsys)["error"]
    assert not (tmp_path / "out").exists()


def test_dry_run_counts_segment_requests_without_key(tmp_path, monkeypatch, capsys):
    """Defect class: a dry run that needs a key, calls a provider, or miscounts requests and cost."""
    packet, _key = _packet_and_key()
    plan = derive_segment_plan(packet, suite_id="ulp", protocol_sha256="0" * 64, denominator={"items": 2, "points": 2})
    _forbid_client(monkeypatch, api_anthropic)
    config = _config("anthropic-api", "claude-opus-5-5", repeats=3)
    assert api_anthropic.main([
        "suite", "--config", str(_write(tmp_path, "c.json", config)), "--questions",
        str(_write(tmp_path, "q.json", packet)), "--segment-plan", str(_write(tmp_path, "p.json", plan)),
        "--batch", "--dry-run"]) == 0
    report = _last_json(capsys)
    assert (report["status"], report["requests"], report["mode"]) == ("dry_run", 6, "batch")
    # Batch price is half of Opus 5.5's $4/$20 list price; output is bounded by max_output_tokens.
    assert report["usd_per_mtok"] == {"input": "2.0", "output": "10.0"}
    expected = (Decimal(report["estimated_input_tokens"]) * 2 + 6 * 2000 * 10) / Decimal(1_000_000)
    assert report["estimated_usd_upper_bound"] == pytest.approx(float(expected))
    with pytest.raises(ValueError, match="price is unknown"):
        api_suite.prices(_config("anthropic-api", "claude-unpriced"))


def test_anthropic_batch_submit_collect_maps_out_of_order_and_failed_results(tmp_path, monkeypatch, capsys):
    """Defect class: batch results attached to the wrong request, or a failed entry scored as ok."""
    packet, key = _packet_and_key()
    plan = derive_segment_plan(packet, suite_id="ulp", protocol_sha256="0" * 64, denominator={"items": 2, "points": 2})
    client = FakeAnthropic()
    _use_client(monkeypatch, api_anthropic, client)
    out = tmp_path / "out"
    assert api_anthropic.main([
        "suite", "--config", str(_write(tmp_path, "c.json", _config("anthropic-api", "claude-opus-5-5", repeats=2))),
        "--questions", str(_write(tmp_path, "q.json", packet)), "--segment-plan",
        str(_write(tmp_path, "p.json", plan)), "--out-dir", str(out), "--max-usd", "5", "--batch"]) == 0
    assert _last_json(capsys)["batch_id"] == "msgbatch_1"
    assert [request["custom_id"] for request in client.batch_requests] == [
        "r001-seg-0001", "r001-seg-0002", "r002-seg-0001", "r002-seg-0002"]
    assert api_anthropic.main(["collect", "--out-dir", str(out)]) == 3
    assert _last_json(capsys)["status"] == "in_progress"
    client.status = "ended"
    client.batch_outcomes = {"r002-seg-0002": "errored"}
    # One entry without an id and one duplicated id must not discard the other valid results.
    duplicate = {"custom_id": "r002-seg-0001", "result": {"type": "succeeded",
                                                          "message": _claude_message(client.batch_requests[2]["params"])}}
    client.extra_entries = [{"result": {"type": "succeeded"}}, duplicate,
                            {"custom_id": "r009-x", "result": {"type": []}}]
    client.extra_lines = ["not json", ""]
    assert api_anthropic.main(["collect", "--out-dir", str(out)]) == 2
    summary = _last_json(capsys)
    # Unattributed: the id-less entry, both duplicates, the unparseable line and the unknown id.
    assert (summary["ok"], summary["failed"], summary["unattributed_entries"]) == (2, 2, 5)
    assert len(json.loads((out / "batch-unattributed.json").read_text())) == 5
    first = json.loads((out / "runs" / "repeat-001.json").read_text())
    assert first["status"] == "ok" and score_run(packet, key, first)["raw_points"] == 2
    assert first["identity"]["harness"] == "anthropic-message-batches"
    assert first["metrics"]["cost_usd"] == pytest.approx(2 * float((Decimal(1000) * 2 + Decimal(500) * 10) / 10**6))
    second = json.loads((out / "runs" / "repeat-002.json").read_text())
    assert second["status"] == "failed" and second["failure_reason"] == "batch_duplicate_result"
    assert list(second["responses"].values()) == [None, None]
    assert json.loads((out / "rows" / "r002-seg-0002.json").read_text())["failure_reason"] == "batch_errored"


def test_openai_batch_uploads_responses_jsonl_and_reports_missing_results(tmp_path, monkeypatch, capsys):
    """Defect class: a batch line with the wrong endpoint or body, or a lost request counted as success."""
    packet, _key = _packet_and_key()
    uploads: list[bytes] = []
    state = {"status": "in_progress"}

    def batch_lines() -> list[dict]:
        return [json.loads(line) for line in uploads[0].decode().splitlines()]

    def content(file_id):
        rows = batch_lines()
        if file_id == "out":
            row = rows[0]
            return SimpleNamespace(text=json.dumps({"custom_id": row["custom_id"], "error": None, "response": {
                "status_code": 200, "body": _openai_response(row["body"])}}) + "\n")
        return SimpleNamespace(text=json.dumps({"custom_id": rows[1]["custom_id"], "error": {"code": "x"},
                                                "response": {"status_code": 400, "body": {}}}) + "\nnot json\n")

    client = SimpleNamespace(
        files=SimpleNamespace(create=lambda file, purpose: uploads.append(file[1]) or {"id": "file_1"},
                              content=content),
        batches=SimpleNamespace(
            create=lambda **kwargs: {"id": "batch_1", **kwargs},
            retrieve=lambda _id: {"id": _id, "status": state["status"], "output_file_id": "out",
                                  "error_file_id": "err"}))
    _use_client(monkeypatch, api_openai, client)
    out = tmp_path / "out"
    config = _config("openai-api", "gpt-6.1-sol", repeats=3)
    assert api_openai.main(["suite", "--config", str(_write(tmp_path, "c.json", config)), "--questions",
                            str(_write(tmp_path, "q.json", packet)), "--out-dir", str(out),
                            "--max-usd", "5", "--batch"]) == 0
    capsys.readouterr()
    lines = batch_lines()
    assert [(line["method"], line["url"]) for line in lines] == [("POST", "/v1/responses")] * 3
    prompt = build_prompt(packet, "closed-book", max_tool_calls=1)
    assert lines[0]["body"] == api_openai.build_request(packet, api_suite.validate_api_config(config), prompt)
    state["status"] = "completed"
    assert api_openai.main(["collect", "--out-dir", str(out)]) == 2
    summary = _last_json(capsys)
    assert (summary["ok"], summary["failed"], summary["unattributed_entries"]) == (1, 2, 1)
    reasons = [json.loads((out / "rows" / f"r00{index}-all.json").read_text())["failure_reason"] for index in (1, 2, 3)]
    assert reasons == [None, "http_status_400", "batch_missing_result"]
    ok_row = json.loads((out / "rows" / "r001-all.json").read_text())
    assert ok_row["identity"]["effective_model"] == "gpt-6.1-sol-2026-11-01"
    assert ok_row["metrics"]["cost_usd"] == pytest.approx(float((Decimal(1000) * 1 + Decimal(500) * 5) / 10**6))


def test_failed_answer_still_records_billed_usage_and_model_drift_fails(tmp_path, monkeypatch, capsys):
    """Defect class: paid tokens lost from the record when a request fails, or a substituted model accepted."""
    packet, _key = _packet_and_key()
    replies = iter([
        lambda params: _claude_message(params, stop="max_tokens", answers={}),
        lambda params: _claude_message(params, model="claude-sonnet-5-5"),
        lambda params: _claude_message(params, model="claude-opus-5-5-other-model"),
        lambda params: _claude_message(params, model="claude-opus-5-5-20261101"),
    ])
    client = FakeAnthropic(reply=lambda params: next(replies)(params))
    _use_client(monkeypatch, api_anthropic, client)
    config = _config("anthropic-api", "claude-opus-5-5", repeats=4)
    assert api_anthropic.main(["suite", "--config", str(_write(tmp_path, "c.json", config)), "--questions",
                               str(_write(tmp_path, "q.json", packet)), "--out-dir", str(tmp_path / "out"),
                               "--max-usd", "1"]) == 2
    limited = json.loads((tmp_path / "out" / "rows" / "r001-all.json").read_text())
    assert limited["status"] == "failed" and limited["failure_reason"] == "output_error"
    assert limited["metrics"]["output_tokens"] == 500 and limited["metrics"]["cost_usd"] > 0
    drifted = json.loads((tmp_path / "out" / "rows" / "r002-all.json").read_text())
    assert drifted["status"] == "failed" and drifted["failure_reason"] == "model_error"
    suffixed = json.loads((tmp_path / "out" / "rows" / "r003-all.json").read_text())
    assert suffixed["status"] == "failed" and suffixed["failure_reason"] == "model_error"
    snapshot = json.loads((tmp_path / "out" / "rows" / "r004-all.json").read_text())
    assert snapshot["status"] == "ok" and snapshot["identity"]["effective_model"] == "claude-opus-5-5-20261101"


def test_runner_path_requires_trial_cap_and_returns_standard_run(monkeypatch):
    """Defect class: the existing runner reaching a paid API without a cost cap or recorded settings."""
    packet, key = _packet_and_key()
    client = FakeAnthropic()
    _use_client(monkeypatch, api_anthropic, client)
    uncapped = run_exam(packet, _config("anthropic-api", "claude-opus-5-5"), "closed-book")
    assert uncapped["status"] == "failed" and client.created == []
    capped = run_exam(packet, _config("anthropic-api", "claude-opus-5-5", max_usd_per_trial=1), "closed-book")
    assert capped["status"] == "ok" and score_run(packet, key, capped)["raw_points"] == 2
    assert capped["identity"]["max_output_tokens_configured"] == 2000
    assert capped["metrics"]["cost_usd"] == pytest.approx(float((Decimal(1000) * 4 + Decimal(500) * 20) / 10**6))
    sources = run_exam(packet, _config("anthropic-api", "claude-opus-5-5", max_usd_per_trial=1), "sources",
                       sources_url="http://127.0.0.1:9")
    assert sources["status"] == "failed" and sources["failure_reason"] == "capability_error"
    assert len(client.created) == 1
    # A billed response whose answers fail keeps its usage and cost in the failed run.
    _use_client(monkeypatch, api_anthropic, FakeAnthropic(
        reply=lambda params: _claude_message(params, stop="max_tokens", answers={})))
    limited = run_exam(packet, _config("anthropic-api", "claude-opus-5-5", max_usd_per_trial=1), "closed-book")
    assert limited["status"] == "failed" and limited["failure_reason"] == "output_error"
    assert limited["metrics"]["output_tokens"] == 500 and limited["metrics"]["cost_usd"] > 0
    assert set(limited["responses"]) == {item["id"] for item in packet["items"]}
