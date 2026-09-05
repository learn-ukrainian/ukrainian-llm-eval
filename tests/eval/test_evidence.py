from __future__ import annotations

import contextlib
import hashlib
import json
import stat
import threading
from pathlib import Path

import pytest

import ukrainian_llm_eval.evidence as evidence_module
from ukrainian_llm_eval.evidence import (
    EVENT_SCHEMA,
    EvidenceError,
    EvidenceIntegrityError,
    EvidenceStore,
)


def _records(store_root: Path, attempt_id: str) -> list[dict[str, object]]:
    path = store_root / "attempts" / attempt_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_start_append_and_finalize_round_trip_is_private_and_snapshot_based(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    store = EvidenceStore(root)
    metadata = {"denominator": 3, "nested": {"value": 1}}
    attempt = store.start(metadata, attempt_id="attempt-one")
    payload = {"explicit": "private value", "nested": {"value": 2}}
    attempt.append("completion_body", payload)
    metadata["nested"]["value"] = 99
    payload["nested"]["value"] = 99

    incomplete = attempt.verify()
    assert incomplete["status"] == "incomplete"
    assert incomplete["metadata"] == {"denominator": 3, "nested": {"value": 1}}

    receipt = attempt.finalize({"score": 0.75, "raw": "provided by caller"})
    assert receipt["status"] == "complete"
    assert receipt["terminal_status"] == "completed"
    assert receipt["result"] == {"score": 0.75, "raw": "provided by caller"}
    assert receipt["denominator"] == 3
    assert receipt["event_count"] == 2

    records = _records(root, attempt.id)
    assert [record["schema"] for record in records] == [EVENT_SCHEMA, EVENT_SCHEMA]
    assert [record["sequence"] for record in records] == [1, 2]
    assert len({record["event_id"] for record in records}) == 2
    assert records[1]["payload"] == {"explicit": "private value", "nested": {"value": 2}}
    for record in records:
        body = {key: value for key, value in record.items() if key != "record_sha256"}
        expected = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        assert record["record_sha256"] == expected

    for path in (root, root / "attempts", root / "attempts" / attempt.id):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (root / "attempts" / attempt.id / "events.jsonl", root / "attempts" / attempt.id / "final.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_attempts_are_unique_and_finalization_cannot_overwrite(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    first = store.start_attempt(1)
    second = store.start_attempt(1)
    assert first.id != second.id
    first_receipt = first.finalize({"answer": "first"})
    final_path = store.root / "attempts" / first.id / "final.json"
    original_final = final_path.read_bytes()

    with pytest.raises(EvidenceError):
        store.start({"denominator": 1}, attempt_id=first.id)
    with pytest.raises(EvidenceError):
        first.finalize({"answer": "replacement"})
    assert final_path.read_bytes() == original_final
    assert store.list_attempts() == sorted([first.id, second.id])
    assert store.verify(first.id) == first_receipt


def test_waiting_append_cannot_extend_an_attempt_after_finalize_wins_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    attempt = store.start({"denominator": 1}, attempt_id="ordered")
    finalizer_entered = threading.Event()
    append_waiting = threading.Event()
    release_finalizer = threading.Event()
    append_errors: list[BaseException] = []
    finalizer_receipts: list[dict[str, object]] = []
    original_locked_events = evidence_module._locked_events

    @contextlib.contextmanager
    def controlled_lock(path: Path, attempt_id: str):
        current_name = threading.current_thread().name
        if current_name == "append":
            append_waiting.set()
        with original_locked_events(path, attempt_id) as fd:
            if current_name == "finalizer":
                finalizer_entered.set()
                assert release_finalizer.wait(timeout=5)
            yield fd

    monkeypatch.setattr(evidence_module, "_locked_events", controlled_lock)

    def finalize() -> None:
        finalizer_receipts.append(attempt.finalize({"score": 1}))

    def append() -> None:
        try:
            attempt.append("late_event", {"should": "be rejected"})
        except EvidenceError as exc:
            append_errors.append(exc)

    finalizer_thread = threading.Thread(target=finalize, name="finalizer")
    append_thread = threading.Thread(target=append, name="append")
    finalizer_thread.start()
    assert finalizer_entered.wait(timeout=5)
    append_thread.start()
    assert append_waiting.wait(timeout=5)
    release_finalizer.set()
    finalizer_thread.join(timeout=5)
    append_thread.join(timeout=5)

    assert not finalizer_thread.is_alive()
    assert not append_thread.is_alive()
    assert len(finalizer_receipts) == 1
    assert append_errors and isinstance(append_errors[0], EvidenceError)
    assert store.verify(attempt.id)["event_count"] == 1


def test_incomplete_attempt_can_resume_and_interrupted_marker_is_detected(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    incomplete = store.start({"denominator": 2}, attempt_id="resume-me")
    incomplete.append("trial_input", {"index": 0})
    before_resume = store.verify("resume-me")
    resumed = store.resume("resume-me")
    assert resumed.id == incomplete.id
    resumed.append("trial_failure", {"reason": "timeout"})
    completed = resumed.finalize({"completed": 1})
    assert completed["event_count"] == before_resume["event_count"] + 1

    interrupted = store.start({"denominator": 1}, attempt_id="interrupted")
    interrupted.append("attempt_interrupted", {"reason": "process exited"})
    interrupted_receipt = store.verify("interrupted")
    assert interrupted_receipt["status"] == "interrupted"
    assert interrupted_receipt["complete"] is False
    with pytest.raises(EvidenceError):
        interrupted.append("late_event", {})
    with pytest.raises(EvidenceError):
        store.resume("interrupted")


def test_tampering_with_event_or_final_record_fails_verification(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    event_attempt = store.start({"denominator": 1}, attempt_id="event-tamper")
    event_attempt.append("response", {"text": "original"})
    event_attempt.finalize({"score": 1})
    events_path = store.root / "attempts" / "event-tamper" / "events.jsonl"
    events_path.write_text(events_path.read_text(encoding="utf-8").replace("original", "changed"), encoding="utf-8")
    with pytest.raises(EvidenceIntegrityError):
        store.verify("event-tamper")

    final_attempt = store.start({"denominator": 1}, attempt_id="final-tamper")
    final_attempt.finalize({"score": 1})
    final_path = store.root / "attempts" / "final-tamper" / "final.json"
    final_path.write_text(final_path.read_text(encoding="utf-8").replace('"score":1', '"score":2'), encoding="utf-8")
    with pytest.raises(EvidenceIntegrityError):
        store.verify("final-tamper")


def test_path_traversal_and_symlink_paths_are_rejected_without_following(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    attempt = store.start({"denominator": 1}, attempt_id="safe")
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(EvidenceError):
        store.verify("../outside")

    final_path = store.root / "attempts" / attempt.id / "final.json"
    final_path.symlink_to(outside)
    with pytest.raises(EvidenceError):
        attempt.finalize({"score": 1})
    assert outside.read_text(encoding="utf-8") == "outside"
    final_path.unlink()

    events_path = store.root / "attempts" / attempt.id / "events.jsonl"
    events_path.unlink()
    events_path.symlink_to(outside)
    with pytest.raises(EvidenceIntegrityError):
        store.verify(attempt.id)
    assert outside.read_text(encoding="utf-8") == "outside"

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(EvidenceError):
        EvidenceStore(linked_root)


def test_missing_denominator_and_implicit_environment_data_are_rejected_or_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UKRAINIAN_LLM_EVAL_SECRET", "must not be copied")
    store = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(EvidenceError):
        store.start({"metadata_only": True})
    attempt = store.start({"denominator": 0}, attempt_id="no-secret")
    receipt = attempt.finalize({"explicit": "caller supplied"})
    assert "UKRAINIAN_LLM_EVAL_SECRET" not in receipt["metadata"]
    assert receipt["result"] == {"explicit": "caller supplied"}
