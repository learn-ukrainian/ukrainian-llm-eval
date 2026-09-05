"""Attempt lifecycle tests: failures and interruption remain in the denominator."""

import pytest

from ukrainian_llm_eval import execution
from ukrainian_llm_eval.evidence import EvidenceStore


def test_failed_execution_has_private_verifiable_evidence(monkeypatch, tmp_path):
    result = {"status": "failed", "failure_reason": "model_drift"}

    def trial(*args, evidence, **kwargs):
        evidence("completion_response", {"raw": "rejected answer"})
        return result

    monkeypatch.setattr(execution, "run_exam", trial)
    root = tmp_path / "evidence"
    observed, receipt = execution.execute_attempt(
        {"packet_sha256": "a" * 64, "items": [{"id": "q0001"}]},
        {"model": "fixture"}, "closed-book", root,
    )
    assert observed == result
    assert receipt["terminal_status"] == "failed"
    assert receipt["denominator"] == 1
    assert EvidenceStore(root).verify(receipt["attempt_id"])["result"] == result


def test_interrupted_execution_is_not_discarded_or_finalized(monkeypatch, tmp_path):
    def trial(*args, evidence, **kwargs):
        evidence("prompt", "question")
        raise KeyboardInterrupt

    monkeypatch.setattr(execution, "run_exam", trial)
    root = tmp_path / "evidence"
    with pytest.raises(KeyboardInterrupt):
        execution.execute_attempt(
            {"packet_sha256": "a" * 64, "items": [{"id": "q0001"}]},
            {"model": "fixture"}, "closed-book", root,
        )
    receipts = EvidenceStore(root).verify_all()
    assert len(receipts) == 1
    receipt = next(iter(receipts.values()))
    assert receipt["status"] == "incomplete"
    assert receipt["denominator"] == 1
    assert receipt["event_count"] == 2
    assert "result" not in receipt
