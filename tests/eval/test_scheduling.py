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


def test_resume_restores_results_without_repeating_calls(monkeypatch, tmp_path):
    packet, config = inputs()
    calls = []
    monkeypatch.setattr(scheduling, "preflight", lambda *args: {})

    def trial(*args, evidence, **kwargs):
        calls.append(args[2])
        evidence("response", "A")
        return {"status": "ok", "responses": {"q0001": "A"}}

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
        return {"status": "ok", "responses": {"q0001": "A"}}

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
