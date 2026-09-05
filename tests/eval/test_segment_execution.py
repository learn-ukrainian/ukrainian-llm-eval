import pytest

from ukrainian_llm_eval import execution
from ukrainian_llm_eval.core import ExamError
from ukrainian_llm_eval.evidence import EvidenceStore


def test_segment_reservation_is_durable_before_provider_and_not_candidate_input(monkeypatch, tmp_path):
    packet = {"packet_sha256": "a" * 64, "items": [{"id": "q0001"}]}
    config = {"adapter": "claude"}
    context = {"execution_plan_sha256": "b" * 64, "cell_id": "c1", "segment_id": "s1",
               "reservation_id": "r1", "reserved_micro_usd": 12}

    def interrupted(candidate_packet, candidate_config, condition, *, sources_url, evidence):
        receipt = EvidenceStore(tmp_path / "evidence").verify("a1")
        assert receipt["metadata"]["segment_context"] == context
        assert receipt["complete"] is False
        assert candidate_packet == packet and candidate_config == config
        assert condition == "closed-book" and sources_url is None
        evidence("prompt", "synthetic question")
        raise KeyboardInterrupt

    monkeypatch.setattr(execution, "run_exam", interrupted)
    with pytest.raises(KeyboardInterrupt):
        execution.execute_attempt(packet, config, "closed-book", tmp_path / "evidence",
                                  attempt_id="a1", segment_context=context)
    receipt = EvidenceStore(tmp_path / "evidence").verify("a1")
    assert receipt["metadata"]["segment_context"]["reserved_micro_usd"] == 12
    assert receipt["complete"] is False


@pytest.mark.parametrize("change", [{"key": {"answers": "must not enter execution"}},
                                  {"reserved_micro_usd": True}, {"reserved_micro_usd": -1},
                                  {"execution_plan_sha256": "bad"}, {"segment_id": "../other"}])
def test_invalid_segment_context_rejected_without_attempt_or_provider(monkeypatch, tmp_path, change):
    context = {"execution_plan_sha256": "b" * 64, "cell_id": "c1", "segment_id": "s1",
               "reservation_id": "r1", "reserved_micro_usd": 12, **change}
    monkeypatch.setattr(execution, "run_exam", lambda *a, **kw: pytest.fail("provider called"))
    with pytest.raises(ExamError):
        execution.execute_attempt({"packet_sha256": "a" * 64, "items": []}, {"adapter": "claude"},
                                  "closed-book", tmp_path / "evidence", segment_context=context)
    assert not (tmp_path / "evidence").exists()
