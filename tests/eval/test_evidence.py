from __future__ import annotations

import contextlib
import hashlib
import json
import os
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


def test_verifier_recovers_only_the_exact_linked_pending_finalization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    attempt = store.start({"denominator": 1}, attempt_id="crash-window")
    original_unlink = Path.unlink
    injected = False

    def fail_after_link(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal injected
        if path.name.startswith(".final-") and not injected:
            injected = True
            raise RuntimeError("injected crash after final link")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_after_link)
    with pytest.raises(RuntimeError, match="injected crash"):
        attempt.finalize({"score": 1})
    assert injected
    final_path = store.root / "attempts" / attempt.id / "final.json"
    pending_paths = list((final_path.parent).glob(".final-*.tmp"))
    assert len(pending_paths) == 1
    assert os.stat(final_path).st_ino == os.stat(pending_paths[0]).st_ino
    assert os.stat(final_path).st_nlink == 2

    receipt = store.verify(attempt.id)
    assert receipt["status"] == "complete"
    assert not list(final_path.parent.glob(".final-*.tmp"))
    assert os.stat(final_path).st_nlink == 1

    rejected = store.start({"denominator": 1}, attempt_id="foreign-pending")
    rejected.finalize({"score": 1})
    rejected_final = store.root / "attempts" / rejected.id / "final.json"
    foreign_pending = rejected_final.parent / ".final-0123456789abcdef0123456789abcdef.tmp"
    foreign_pending.write_text("foreign", encoding="utf-8")
    foreign_pending.chmod(0o600)
    with pytest.raises(EvidenceIntegrityError):
        store.verify(rejected.id)
    assert foreign_pending.exists()


def test_verifier_publishes_valid_prelink_pending_final_and_rejects_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    attempt = store.start({"denominator": 1}, attempt_id="prelink-window")
    original_link = evidence_module.os.link
    original_unlink = Path.unlink
    link_injected = False
    unlink_injected = False

    def fail_before_link(source: Path, destination: Path, *args: object, **kwargs: object) -> None:
        nonlocal link_injected
        if Path(source).name.startswith(".final-") and Path(destination).name == "final.json" and not link_injected:
            link_injected = True
            raise RuntimeError("injected crash before final link")
        original_link(source, destination, *args, **kwargs)

    def fail_pending_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal unlink_injected
        if path.name.startswith(".final-") and not unlink_injected:
            unlink_injected = True
            raise RuntimeError("injected crash during pre-link cleanup")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(evidence_module.os, "link", fail_before_link)
    monkeypatch.setattr(Path, "unlink", fail_pending_cleanup)
    with pytest.raises(RuntimeError, match="pre-link cleanup"):
        attempt.finalize({"score": 1})
    assert link_injected and unlink_injected
    attempt_dir = store.root / "attempts" / attempt.id
    pending_paths = list(attempt_dir.glob(".final-*.tmp"))
    assert len(pending_paths) == 1
    assert not (attempt_dir / "final.json").exists()
    assert os.stat(pending_paths[0]).st_nlink == 1

    receipt = store.verify(attempt.id)
    assert receipt["status"] == "complete"
    assert (attempt_dir / "final.json").exists()
    assert not list(attempt_dir.glob(".final-*.tmp"))

    multiple = store.start({"denominator": 1}, attempt_id="multiple-pending")
    multiple.finalize({"score": 1})
    multiple_dir = store.root / "attempts" / multiple.id
    original_final = multiple_dir / "final.json"
    first_pending = multiple_dir / ".final-0123456789abcdef0123456789abcdef.tmp"
    original_final.rename(first_pending)
    second_pending = multiple_dir / ".final-fedcba9876543210fedcba9876543210.tmp"
    second_pending.write_bytes(first_pending.read_bytes())
    second_pending.chmod(0o600)
    with pytest.raises(EvidenceIntegrityError):
        store.verify(multiple.id)
    assert first_pending.exists()
    assert second_pending.exists()
    assert not original_final.exists()


def test_invalid_duplicate_final_metadata_is_not_cleaned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    attempt = store.start({"denominator": 1}, attempt_id="invalid-duplicate")
    original_unlink = Path.unlink
    injected = False

    def fail_after_link(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal injected
        if path.name.startswith(".final-") and not injected:
            injected = True
            raise RuntimeError("injected crash after final link")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_after_link)
    with pytest.raises(RuntimeError, match="injected crash"):
        attempt.finalize({"score": 1})
    assert injected
    final_path = store.root / "attempts" / attempt.id / "final.json"
    pending_path = next(final_path.parent.glob(".final-*.tmp"))
    final_record = json.loads(final_path.read_text(encoding="utf-8"))
    final_record["event_count"] = 99
    body = {key: value for key, value in final_record.items() if key != "final_sha256"}
    final_record["final_sha256"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    final_path.write_text(
        json.dumps(final_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceIntegrityError):
        store.verify(attempt.id)
    assert pending_path.exists()
    assert os.stat(final_path).st_nlink == 2


def test_store_requires_posix_ownership_and_file_locking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence_module, "fcntl", None)
    with pytest.raises(EvidenceError, match="POSIX ownership and file-locking"):
        EvidenceStore(tmp_path / "unsupported")


def test_inspect_all_reports_corruption_and_foreign_entries_without_hiding_valid_attempts(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    valid = store.start({"denominator": 1}, attempt_id="valid")
    valid.finalize({"score": 1})
    corrupt = store.start({"denominator": 1}, attempt_id="corrupt")
    corrupt.finalize({"score": 1})
    corrupt_final = store.root / "attempts" / corrupt.id / "final.json"
    corrupt_final.write_text(corrupt_final.read_text(encoding="utf-8").replace('"score":1', '"score":2'), encoding="utf-8")
    foreign = store.root / "attempts" / "foreign.txt"
    foreign.write_text("unrecognized entry", encoding="utf-8")

    reports = store.inspect_all()
    assert reports["valid"]["status"] == "complete"
    assert reports["corrupt"]["status"] == "corrupt"
    assert reports["corrupt"]["error_class"] == "EvidenceIntegrityError"
    assert reports["foreign.txt"]["status"] == "corrupt"
    assert reports["foreign.txt"]["error_class"] == "EvidenceIntegrityError"
    assert foreign.read_text(encoding="utf-8") == "unrecognized entry"
    with pytest.raises(EvidenceIntegrityError):
        store.verify_all()


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
