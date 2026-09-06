import json

import pytest

from ukrainian_llm_eval import execution, scheduling
from ukrainian_llm_eval.core import ExamError
from ukrainian_llm_eval.evidence import EvidenceStore


def inputs():
    packet = {"schema": "zno-nmt.questions.v1", "packet_sha256": "a" * 64,
              "items": [{"id": "q0001", "kind": "single", "options": [{"id": "A", "text": "один"}], "rows": []}]}
    config = {"adapter": "claude", "model": "fixture", "effort": None, "timeout_seconds": 10,
              "max_output_tokens": 100, "max_tool_calls": 2, "repeats": 1, "tools": [], "corpus_id": None}
    return packet, config


def _success(session):
    return {"status": "ok", "identity": {"session_id": session}, "responses": {"q0001": "A"}}


def test_resume_restores_results_without_repeating_calls(monkeypatch, tmp_path):
    packet, config = inputs()
    calls = []
    monkeypatch.setattr(scheduling, "preflight", lambda *args: {})

    def trial(*args, evidence, **kwargs):
        calls.append(args[2])
        evidence("response", "A")
        return _success(f"session-{len(calls)}")

    monkeypatch.setattr(execution, "run_exam", trial)
    root = tmp_path / "pair"
    assert len(list(scheduling.run_pair(packet, config, root))) == 2
    before = (root / "001-closed-book.json").read_bytes()
    resumed = list(scheduling.run_pair(packet, config, root, resume=True))
    assert len(calls) == 2
    assert all(item["resumed"] for item in resumed)
    assert (root / "001-closed-book.json").read_bytes() == before
    with pytest.raises(ExamError, match="frozen schedule"):
        list(scheduling.run_pair(packet, {**config, "model": "different"}, root, resume=True))
    assert len(calls) == 2


def test_resume_accounts_for_interruption_and_runs_only_unstarted_slot(monkeypatch, tmp_path):
    packet, config = inputs()
    monkeypatch.setattr(scheduling, "preflight", lambda *args: {})
    calls = []

    def trial(*args, evidence, **kwargs):
        calls.append(args[2])
        evidence("prompt", "question")
        if len(calls) == 1:
            raise KeyboardInterrupt
        return _success(f"session-{len(calls)}")

    monkeypatch.setattr(execution, "run_exam", trial)
    root = tmp_path / "pair"
    with pytest.raises(KeyboardInterrupt):
        list(scheduling.run_pair(packet, config, root))
    receipts = EvidenceStore(root / "evidence").verify_all()
    assert receipts["r001-closed-book"]["status"] == "incomplete"
    resumed = list(scheduling.run_pair(packet, config, root, resume=True))
    assert calls == ["closed-book", "sources"]
    assert resumed[0]["status"] == "failed"
    assert resumed[-1]["failed"] is True
    receipts = EvidenceStore(root / "evidence").verify_all()
    assert len(receipts) == 2
    assert receipts["r001-closed-book"]["terminal_status"] == "interrupted"
    assert receipts["r001-closed-book"]["result"]["failure_reason"] == "interrupted"


def test_resume_rejects_changed_resolved_endpoints_before_preflight(monkeypatch, tmp_path):
    packet, config = inputs()
    config = {**config, "adapter": "chat-http", "endpoint_env": "TEST_COMPLETION_URL"}
    monkeypatch.setenv("TEST_COMPLETION_URL", "https://original.invalid/chat")
    preflights = []
    calls = []
    monkeypatch.setattr(scheduling, "preflight", lambda *args: preflights.append(args))

    def trial(*args, **kwargs):
        calls.append(args[2])
        return _success(f"session-{len(calls)}")

    monkeypatch.setattr(execution, "run_exam", trial)
    root = tmp_path / "pair"
    list(scheduling.run_pair(packet, config, root, sources_url="https://original.invalid/mcp"))
    count = len(preflights)
    with pytest.raises(ExamError, match="frozen schedule"):
        list(scheduling.run_pair(packet, config, root, sources_url="https://different.invalid/mcp", resume=True))
    monkeypatch.setenv("TEST_COMPLETION_URL", "https://different.invalid/chat")
    with pytest.raises(ExamError, match="frozen schedule"):
        list(scheduling.run_pair(packet, config, root, sources_url="https://original.invalid/mcp", resume=True))
    assert len(preflights) == count
    assert "original.invalid" not in (root / "plan.json").read_text()


def test_pair_missing_success_session_stops_before_followup_execution(monkeypatch, tmp_path):
    packet, config = inputs()
    config = {**config, "repeats": 2}
    calls = []
    preflights = []
    monkeypatch.setattr(scheduling, "preflight", lambda *args: preflights.append(args))

    def trial(*args, evidence, **kwargs):
        calls.append(args[2])
        evidence("response", "A")
        return {"status": "ok", "responses": {"q0001": "A"}}

    monkeypatch.setattr(execution, "run_exam", trial)
    root = tmp_path / "pair"
    progress = list(scheduling.run_pair(packet, config, root))

    assert calls == ["closed-book"]
    assert progress == [{"status": "stopped", "reason": "missing_or_reused_session", "failed": True}]
    assert len(preflights) == 2
    receipts = EvidenceStore(root / "evidence").verify_all()
    assert len(receipts) == 1
    assert receipts["r001-closed-book"]["result"]["status"] == "ok"
    assert json.loads((root / "stop.json").read_text())["reason"] == "missing_or_reused_session"

    assert list(scheduling.run_pair(packet, config, root, resume=True)) == progress
    assert calls == ["closed-book"]


def test_pair_generic_failure_without_session_keeps_existing_resume_policy(monkeypatch, tmp_path):
    packet, config = inputs()
    config = {**config, "repeats": 2}
    calls = []
    monkeypatch.setattr(scheduling, "preflight", lambda *args: {})

    def trial(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 1:
            return {"status": "failed", "failure_reason": "transport",
                    "responses": {"q0001": None}}
        return _success(f"session-{len(calls)}")

    monkeypatch.setattr(execution, "run_exam", trial)
    root = tmp_path / "pair"
    first = list(scheduling.run_pair(packet, config, root))
    assert len(calls) == 1 and first[0]["status"] == "failed"
    assert not (root / "stop.json").exists()

    resumed = list(scheduling.run_pair(packet, config, root, resume=True))
    assert len(calls) == 4
    assert all(item["status"] in {"failed", "ok"} for item in resumed)
    assert not (root / "stop.json").exists()


def test_pair_resume_validates_stop_receipt_binding(monkeypatch, tmp_path):
    packet, config = inputs()
    config = {**config, "repeats": 2}
    calls = []
    monkeypatch.setattr(scheduling, "preflight", lambda *args: {})

    def trial(*args, **kwargs):
        calls.append(args[2])
        return {"status": "ok", "responses": {"q0001": "A"}}

    monkeypatch.setattr(execution, "run_exam", trial)
    root = tmp_path / "pair"
    list(scheduling.run_pair(packet, config, root))
    stop = json.loads((root / "stop.json").read_text())
    stop["receipt_sha256"] = "0" * 64
    (root / "stop.json").write_text(json.dumps(stop))

    with pytest.raises(ExamError, match="receipt binding"):
        list(scheduling.run_pair(packet, config, root, resume=True))
    assert calls == ["closed-book"]
