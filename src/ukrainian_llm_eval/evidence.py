"""Private, append-only execution evidence for local evaluator runs.

This module deliberately has no provider, network, credential, or environment
lookup behavior.  Callers provide the payloads they want retained.  Records
are JSON objects whose canonical representation is covered by a SHA-256 hash
and a per-attempt hash chain.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not expose fcntl.
    fcntl = None


STORE_SCHEMA: Final = "ukrainian-llm-eval.evidence.v1"
EVENT_SCHEMA: Final = "ukrainian-llm-eval.evidence-event.v1"
FINAL_SCHEMA: Final = "ukrainian-llm-eval.evidence-final.v1"

_EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ATTEMPT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})
_RESERVED_EVENT_KINDS = frozenset({"attempt_started"})
_START_KIND = "attempt_started"
_INTERRUPTED_KIND = "attempt_interrupted"
_EVENT_FIELDS = frozenset(
    {
        "schema",
        "attempt_id",
        "event_id",
        "sequence",
        "kind",
        "recorded_at",
        "previous_sha256",
        "payload",
        "record_sha256",
    }
)
_FINAL_FIELDS = frozenset(
    {
        "schema",
        "attempt_id",
        "status",
        "event_count",
        "last_event_sha256",
        "result",
        "finalized_at",
        "final_sha256",
    }
)


class EvidenceError(ValueError):
    """Raised when a store operation or caller-supplied record is invalid."""


class EvidenceIntegrityError(EvidenceError):
    """Raised when an on-disk record cannot be verified."""


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _current_uid() -> int | None:
    return os.getuid() if hasattr(os, "getuid") else None


def _check_owner(st: os.stat_result, path: Path, *, expected_mode: int) -> None:
    uid = _current_uid()
    if uid is not None and st.st_uid != uid:
        raise EvidenceIntegrityError(f"{path} is not owned by the current user")
    if stat.S_IMODE(st.st_mode) != expected_mode:
        raise EvidenceIntegrityError(
            f"{path} must have mode {expected_mode:o}, got {_mode(path):o}"
        )


def _check_dir(path: Path, label: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise EvidenceIntegrityError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise EvidenceIntegrityError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise EvidenceIntegrityError(f"{label} is not a directory: {path}")
    _check_owner(st, path, expected_mode=0o700)


def _ensure_private_dir(path: Path, label: str) -> None:
    """Create or check this component without resolving arbitrary ancestors.

    The store rejects a symlink at the component it owns and enforces its
    owner-only mode.  Parent components are supplied by the caller and are
    intentionally not recursively resolved or audited here.
    """

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            st = os.lstat(path)
        else:
            st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise EvidenceError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise EvidenceError(f"{label} is not a directory: {path}")
    uid = _current_uid()
    if uid is not None and st.st_uid != uid:
        raise EvidenceError(f"{label} is not owned by the current user: {path}")
    if stat.S_IMODE(st.st_mode) != 0o700:
        try:
            os.chmod(path, 0o700, follow_symlinks=False)
        except OSError as exc:
            raise EvidenceError(f"could not make {label} owner-only: {path}") from exc
    _check_dir(path, label)


def _check_file(path: Path, label: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise EvidenceIntegrityError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise EvidenceIntegrityError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise EvidenceIntegrityError(f"{label} is not a regular file: {path}")
    _check_owner(st, path, expected_mode=0o600)
    if getattr(st, "st_nlink", 1) != 1:
        raise EvidenceIntegrityError(f"{label} must not have additional hard links: {path}")


def _strict_loads(raw: bytes, label: str) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON value {value}")

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceIntegrityError(f"invalid {label} JSON") from exc


def _canonical(value: Any, label: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceError(f"{label} must be JSON serializable") from exc


def _json_copy(value: Any, label: str) -> Any:
    """Validate and copy a caller value before later caller mutation."""

    return _strict_loads(_canonical(value, label).encode("utf-8"), label)


def _hash_body(value: Mapping[str, Any], field: str) -> str:
    body = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(_canonical(body, "record").encode("utf-8")).hexdigest()


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - defensive guard for odd filesystems.
            raise EvidenceError("evidence write made no progress")
        view = view[written:]


def _open_flags(base: int) -> int:
    return base | getattr(os, "O_NOFOLLOW", 0)


def _sync_dir(path: Path) -> None:
    flags = _open_flags(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EvidenceError(f"could not open directory for syncing: {path}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise EvidenceError(f"could not sync directory: {path}") from exc
    finally:
        os.close(fd)


def _parse_events(raw: bytes, attempt_id: str) -> list[dict[str, Any]]:
    if not raw:
        raise EvidenceIntegrityError(f"attempt {attempt_id} has an empty event log")
    if not raw.endswith(b"\n"):
        raise EvidenceIntegrityError(f"attempt {attempt_id} event log is truncated")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.split(b"\n")[:-1], start=1):
        if not line:
            raise EvidenceIntegrityError(
                f"attempt {attempt_id} event log has a blank line at {line_number}"
            )
        item = _strict_loads(line, f"event line {line_number}")
        if not isinstance(item, dict):
            raise EvidenceIntegrityError(f"event line {line_number} is not an object")
        records.append(item)
    return records


def _validate_event(record: Mapping[str, Any], attempt_id: str, sequence: int) -> str:
    if set(record) != _EVENT_FIELDS:
        raise EvidenceIntegrityError(f"attempt {attempt_id} event has unexpected fields")
    if record["schema"] != EVENT_SCHEMA or record["attempt_id"] != attempt_id:
        raise EvidenceIntegrityError(f"attempt {attempt_id} event identity is invalid")
    event_id = record["event_id"]
    if not isinstance(event_id, str) or _EVENT_ID_RE.fullmatch(event_id) is None:
        raise EvidenceIntegrityError(f"attempt {attempt_id} event id is invalid")
    if isinstance(record["sequence"], bool) or not isinstance(record["sequence"], int) or record["sequence"] != sequence:
        raise EvidenceIntegrityError(f"attempt {attempt_id} event sequence is invalid")
    if not isinstance(record["kind"], str) or not record["kind"] or len(record["kind"]) > 128:
        raise EvidenceIntegrityError(f"attempt {attempt_id} event kind is invalid")
    if not isinstance(record["recorded_at"], str) or not record["recorded_at"]:
        raise EvidenceIntegrityError(f"attempt {attempt_id} event timestamp is invalid")
    previous = record["previous_sha256"]
    if previous is not None and (
        not isinstance(previous, str) or re.fullmatch(r"[0-9a-f]{64}", previous) is None
    ):
        raise EvidenceIntegrityError(f"attempt {attempt_id} previous event hash is invalid")
    record_hash = record["record_sha256"]
    if not isinstance(record_hash, str) or re.fullmatch(r"[0-9a-f]{64}", record_hash) is None:
        raise EvidenceIntegrityError(f"attempt {attempt_id} event hash is invalid")
    if _hash_body(record, "record_sha256") != record_hash:
        raise EvidenceIntegrityError(f"attempt {attempt_id} event hash does not match its body")
    return record_hash


def _validate_chain(records: list[dict[str, Any]], attempt_id: str) -> None:
    previous: str | None = None
    for sequence, record in enumerate(records, start=1):
        record_hash = _validate_event(record, attempt_id, sequence)
        if record["previous_sha256"] != previous:
            raise EvidenceIntegrityError(f"attempt {attempt_id} event chain is broken")
        previous = record_hash
    first = records[0]
    if first["kind"] != _START_KIND:
        raise EvidenceIntegrityError(f"attempt {attempt_id} does not start with metadata")
    payload = first["payload"]
    if not isinstance(payload, dict) or set(payload) != {"denominator", "metadata"}:
        raise EvidenceIntegrityError(f"attempt {attempt_id} metadata event is invalid")
    denominator = payload["denominator"]
    if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator < 0:
        raise EvidenceIntegrityError(f"attempt {attempt_id} denominator is invalid")
    if not isinstance(payload["metadata"], dict):
        raise EvidenceIntegrityError(f"attempt {attempt_id} metadata is invalid")


def _validate_final(record: Mapping[str, Any], attempt_id: str) -> None:
    if set(record) != _FINAL_FIELDS:
        raise EvidenceIntegrityError(f"attempt {attempt_id} final record has unexpected fields")
    if record["schema"] != FINAL_SCHEMA or record["attempt_id"] != attempt_id:
        raise EvidenceIntegrityError(f"attempt {attempt_id} final identity is invalid")
    if not isinstance(record["status"], str) or record["status"] not in _TERMINAL_STATUSES:
        raise EvidenceIntegrityError(f"attempt {attempt_id} final status is invalid")
    count = record["event_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise EvidenceIntegrityError(f"attempt {attempt_id} final event count is invalid")
    last_hash = record["last_event_sha256"]
    if not isinstance(last_hash, str) or re.fullmatch(r"[0-9a-f]{64}", last_hash) is None:
        raise EvidenceIntegrityError(f"attempt {attempt_id} final event hash is invalid")
    if not isinstance(record["finalized_at"], str) or not record["finalized_at"]:
        raise EvidenceIntegrityError(f"attempt {attempt_id} final timestamp is invalid")
    final_hash = record["final_sha256"]
    if not isinstance(final_hash, str) or re.fullmatch(r"[0-9a-f]{64}", final_hash) is None:
        raise EvidenceIntegrityError(f"attempt {attempt_id} final hash is invalid")
    if _hash_body(record, "final_sha256") != final_hash:
        raise EvidenceIntegrityError(f"attempt {attempt_id} final hash does not match its body")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@contextlib.contextmanager
def _locked_events(path: Path, attempt_id: str) -> Iterator[int]:
    try:
        fd = os.open(path, _open_flags(os.O_RDWR | os.O_APPEND))
    except OSError as exc:
        raise EvidenceIntegrityError(f"could not open attempt {attempt_id} event log") from exc
    try:
        st = os.fstat(fd)
        _check_owner(st, path, expected_mode=0o600)
        if not stat.S_ISREG(st.st_mode):
            raise EvidenceIntegrityError(f"attempt {attempt_id} event log is not regular")
        if getattr(st, "st_nlink", 1) != 1:
            raise EvidenceIntegrityError(f"attempt {attempt_id} event log has hard links")
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield fd
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_private_file(path: Path, label: str) -> bytes:
    _check_file(path, label)
    try:
        fd = os.open(path, _open_flags(os.O_RDONLY))
    except OSError as exc:
        raise EvidenceIntegrityError(f"could not open {label}: {path}") from exc
    try:
        st = os.fstat(fd)
        _check_owner(st, path, expected_mode=0o600)
        if not stat.S_ISREG(st.st_mode) or getattr(st, "st_nlink", 1) != 1:
            raise EvidenceIntegrityError(f"{label} is not a private regular file: {path}")
        return _read_fd(fd)
    finally:
        os.close(fd)


def _new_private_file(path: Path, label: str) -> int:
    try:
        fd = os.open(
            path,
            _open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
        )
    except FileExistsError as exc:
        raise EvidenceError(f"{label} already exists: {path}") from exc
    except OSError as exc:
        raise EvidenceError(f"could not create {label}: {path}") from exc
    try:
        st = os.fstat(fd)
        _check_owner(st, path, expected_mode=0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _safe_attempt_id(value: str) -> str:
    if not isinstance(value, str) or _ATTEMPT_ID_RE.fullmatch(value) is None:
        raise EvidenceError("attempt id must be a simple lowercase local identifier")
    return value


def _event_payload(records: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    start = records[0]["payload"]
    return start["denominator"], start["metadata"]


def _pending_finalization(attempt_path: Path, attempt_id: str) -> bool:
    pending = False
    for path in attempt_path.glob(".final-*.tmp"):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            raise EvidenceIntegrityError(f"attempt {attempt_id} temporary record must not be a symlink")
        if not stat.S_ISREG(st.st_mode):
            raise EvidenceIntegrityError(f"attempt {attempt_id} temporary record is not regular")
        _check_owner(st, path, expected_mode=0o600)
        if getattr(st, "st_nlink", 1) != 1:
            raise EvidenceIntegrityError(f"attempt {attempt_id} temporary record has hard links")
        pending = True
    return pending


class EvidenceStore:
    """Owner-only local evidence store with immutable finalization."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        _ensure_private_dir(self.root, "evidence root")
        self.attempts_root = self.root / "attempts"
        _ensure_private_dir(self.attempts_root, "attempts directory")

    def _attempt_path(self, attempt_id: str) -> Path:
        safe_id = _safe_attempt_id(attempt_id)
        path = self.attempts_root / safe_id
        _check_dir(path, f"attempt {safe_id}")
        return path

    @staticmethod
    def _events_path(attempt_path: Path) -> Path:
        return attempt_path / "events.jsonl"

    @staticmethod
    def _final_path(attempt_path: Path) -> Path:
        return attempt_path / "final.json"

    def _read_verified_events(self, attempt_id: str) -> tuple[Path, list[dict[str, Any]]]:
        attempt_path = self._attempt_path(attempt_id)
        events_path = self._events_path(attempt_path)
        with _locked_events(events_path, attempt_id) as fd:
            records = _parse_events(_read_fd(fd), attempt_id)
            _validate_chain(records, attempt_id)
        return attempt_path, records

    def _final_if_present(self, attempt_path: Path, attempt_id: str) -> dict[str, Any] | None:
        final_path = self._final_path(attempt_path)
        try:
            os.lstat(final_path)
        except FileNotFoundError:
            return None
        raw = _read_private_file(final_path, f"attempt {attempt_id} final record")
        item = _strict_loads(raw, f"attempt {attempt_id} final record")
        if not isinstance(item, dict):
            raise EvidenceIntegrityError(f"attempt {attempt_id} final record is not an object")
        _validate_final(item, attempt_id)
        return item

    def start(self, metadata: Mapping[str, Any], *, attempt_id: str | None = None) -> Attempt:
        """Create and durably record one attempt's metadata event."""

        if not isinstance(metadata, Mapping):
            raise EvidenceError("attempt metadata must be an object")
        metadata_value = _json_copy(dict(metadata), "attempt metadata")
        if not isinstance(metadata_value, dict):
            raise EvidenceError("attempt metadata must be an object")
        if "denominator" not in metadata_value:
            raise EvidenceError("attempt metadata must include denominator")
        denominator = metadata_value["denominator"]
        if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator < 0:
            raise EvidenceError("attempt denominator must be a non-negative integer")

        if attempt_id is not None:
            safe_id = _safe_attempt_id(attempt_id)
            ids = [safe_id]
        else:
            ids = [uuid.uuid4().hex for _ in range(8)]
        for safe_id in ids:
            attempt_path = self.attempts_root / safe_id
            try:
                attempt_path.mkdir(mode=0o700)
            except FileExistsError:
                if attempt_id is not None:
                    raise EvidenceError(f"attempt {safe_id} already exists") from None
                continue
            _check_dir(attempt_path, f"attempt {safe_id}")
            events_path = self._events_path(attempt_path)
            fd = _new_private_file(events_path, f"attempt {safe_id} event log")
            try:
                record = _start_record_for(metadata_value, safe_id)
                _write_all(fd, (_canonical(record, "event").encode("utf-8") + b"\n"))
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                raise
            else:
                os.close(fd)
            _sync_dir(attempt_path)
            _sync_dir(self.attempts_root)
            return Attempt(self, safe_id)
        raise EvidenceError("could not allocate a unique attempt id")

    def start_attempt(
        self,
        denominator: int,
        metadata: Mapping[str, Any] | None = None,
        *,
        attempt_id: str | None = None,
    ) -> Attempt:
        """Convenience wrapper for callers that keep denominator separately."""

        if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator < 0:
            raise EvidenceError("attempt denominator must be a non-negative integer")
        values = dict(metadata) if metadata is not None else {}
        values["denominator"] = denominator
        return self.start(values, attempt_id=attempt_id)

    def append_event(self, attempt_id: str, kind: str, payload: Any) -> None:
        self._append(_safe_attempt_id(attempt_id), kind, payload)

    def _append(self, attempt_id: str, kind: str, payload: Any) -> None:
        if not isinstance(kind, str) or not kind or len(kind) > 128:
            raise EvidenceError("event kind must be a non-empty string of at most 128 characters")
        if kind in _RESERVED_EVENT_KINDS:
            raise EvidenceError("attempt_started is reserved for store.start")
        payload_value = _json_copy(payload, "event payload")
        attempt_path = self._attempt_path(attempt_id)
        events_path = self._events_path(attempt_path)
        with _locked_events(events_path, attempt_id) as fd:
            final_path = self._final_path(attempt_path)
            if os.path.lexists(final_path):
                raise EvidenceError(f"attempt {attempt_id} is already finalized")
            records = _parse_events(_read_fd(fd), attempt_id)
            _validate_chain(records, attempt_id)
            if records[-1]["kind"] == _INTERRUPTED_KIND:
                raise EvidenceError(f"attempt {attempt_id} is marked interrupted")
            record = _make_event(
                attempt_id=attempt_id,
                sequence=len(records) + 1,
                kind=kind,
                payload=payload_value,
                previous_sha256=records[-1]["record_sha256"],
            )
            _write_all(fd, (_canonical(record, "event").encode("utf-8") + b"\n"))
            os.fsync(fd)
        _sync_dir(attempt_path)

    def finalize(
        self,
        attempt_id: str,
        result: Mapping[str, Any],
        *,
        status: str = "completed",
    ) -> dict[str, Any]:
        if not isinstance(result, Mapping):
            raise EvidenceError("final result must be an object")
        if status not in _TERMINAL_STATUSES:
            raise EvidenceError(f"final status must be one of {sorted(_TERMINAL_STATUSES)}")
        result_value = _json_copy(dict(result), "final result")
        if not isinstance(result_value, dict):
            raise EvidenceError("final result must be an object")
        attempt_id = _safe_attempt_id(attempt_id)
        attempt_path = self._attempt_path(attempt_id)
        final_path = self._final_path(attempt_path)

        events_path = self._events_path(attempt_path)
        with _locked_events(events_path, attempt_id) as fd:
            if os.path.lexists(final_path):
                raise EvidenceError(f"attempt {attempt_id} is already finalized")
            records = _parse_events(_read_fd(fd), attempt_id)
            _validate_chain(records, attempt_id)
            final_record = {
                "schema": FINAL_SCHEMA,
                "attempt_id": attempt_id,
                "status": status,
                "event_count": len(records),
                "last_event_sha256": records[-1]["record_sha256"],
                "result": result_value,
                "finalized_at": _timestamp(),
            }
            final_record["final_sha256"] = _hash_body(final_record, "final_sha256")
            temp_path = attempt_path / f".final-{uuid.uuid4().hex}.tmp"
            fd_temp = _new_private_file(temp_path, f"attempt {attempt_id} finalization temporary")
            try:
                _write_all(fd_temp, _canonical(final_record, "final record").encode("utf-8"))
                os.fsync(fd_temp)
            finally:
                os.close(fd_temp)
            try:
                _check_file(temp_path, f"attempt {attempt_id} finalization temporary")
                try:
                    os.link(temp_path, final_path, follow_symlinks=False)
                except FileExistsError as exc:
                    raise EvidenceError(f"attempt {attempt_id} is already finalized") from exc
                _sync_dir(attempt_path)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    temp_path.unlink()
                raise
            else:
                with contextlib.suppress(FileNotFoundError):
                    temp_path.unlink()
        _sync_dir(attempt_path)
        return self.verify(attempt_id)

    def verify(self, attempt_id: str) -> dict[str, Any]:
        attempt_id = _safe_attempt_id(attempt_id)
        attempt_path, records = self._read_verified_events(attempt_id)
        denominator, metadata = _event_payload(records)
        final_record = self._final_if_present(attempt_path, attempt_id)
        if final_record is None:
            status = "interrupted" if records[-1]["kind"] == _INTERRUPTED_KIND else "incomplete"
            return {
                "schema": STORE_SCHEMA,
                "attempt_id": attempt_id,
                "status": status,
                "complete": False,
                "denominator": denominator,
                "event_count": len(records),
                "last_event_sha256": records[-1]["record_sha256"],
                "metadata": metadata,
                "pending_finalization": _pending_finalization(attempt_path, attempt_id),
            }
        if final_record["event_count"] != len(records):
            raise EvidenceIntegrityError(f"attempt {attempt_id} final event count does not match log")
        if final_record["last_event_sha256"] != records[-1]["record_sha256"]:
            raise EvidenceIntegrityError(f"attempt {attempt_id} final event hash does not match log")
        return {
            "schema": STORE_SCHEMA,
            "attempt_id": attempt_id,
            "status": "complete",
            "complete": True,
            "terminal_status": final_record["status"],
            "denominator": denominator,
            "event_count": len(records),
            "last_event_sha256": records[-1]["record_sha256"],
            "metadata": metadata,
            "result": final_record["result"],
            "final_sha256": final_record["final_sha256"],
        }

    def resume(self, attempt_id: str) -> Attempt:
        receipt = self.verify(attempt_id)
        if receipt["status"] != "incomplete":
            raise EvidenceError(f"attempt {attempt_id} cannot be resumed from status {receipt['status']}")
        return Attempt(self, receipt["attempt_id"])

    def verify_attempt(self, attempt_id: str) -> dict[str, Any]:
        return self.verify(attempt_id)

    def resume_attempt(self, attempt_id: str) -> Attempt:
        return self.resume(attempt_id)

    def finalize_attempt(
        self,
        attempt_id: str,
        result: Mapping[str, Any],
        *,
        status: str = "completed",
    ) -> dict[str, Any]:
        return self.finalize(attempt_id, result, status=status)

    def list_attempts(self) -> list[str]:
        _check_dir(self.attempts_root, "attempts directory")
        names: list[str] = []
        for child in sorted(self.attempts_root.iterdir(), key=lambda path: path.name):
            if child.is_symlink():
                raise EvidenceIntegrityError(f"attempt path must not be a symlink: {child}")
            if not child.is_dir():
                raise EvidenceIntegrityError(f"unexpected path in attempts directory: {child}")
            _safe_attempt_id(child.name)
            _check_dir(child, f"attempt {child.name}")
            names.append(child.name)
        return names

    def verify_all(self) -> dict[str, dict[str, Any]]:
        return {attempt_id: self.verify(attempt_id) for attempt_id in self.list_attempts()}


class Attempt:
    """A handle for appending one attempt and finalizing it exactly once."""

    def __init__(self, store: EvidenceStore, attempt_id: str) -> None:
        self._store = store
        self.id = _safe_attempt_id(attempt_id)

    def append(self, kind: str, payload: Any) -> None:
        self._store._append(self.id, kind, payload)

    def finalize(self, result: Mapping[str, Any], *, status: str = "completed") -> dict[str, Any]:
        return self._store.finalize(self.id, result, status=status)

    def verify(self) -> dict[str, Any]:
        return self._store.verify(self.id)


def _make_event(
    *,
    attempt_id: str,
    sequence: int,
    kind: str,
    payload: Any,
    previous_sha256: str | None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": EVENT_SCHEMA,
        "attempt_id": attempt_id,
        "event_id": uuid.uuid4().hex,
        "sequence": sequence,
        "kind": kind,
        "recorded_at": _timestamp(),
        "previous_sha256": previous_sha256,
        "payload": payload,
    }
    record["record_sha256"] = _hash_body(record, "record_sha256")
    return record


def _start_record_for(metadata: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    return _make_event(
        attempt_id=attempt_id,
        sequence=1,
        kind=_START_KIND,
        payload={"denominator": metadata["denominator"], "metadata": metadata},
        previous_sha256=None,
    )
