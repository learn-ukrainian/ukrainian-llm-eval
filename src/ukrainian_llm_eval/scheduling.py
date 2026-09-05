"""Resume a frozen paired schedule without retrying or dropping started attempts."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from .core import ExamError, digest, read_json, write_private_json
from .evidence import EvidenceStore
from .execution import execute_attempt
from .runner import _failure, preflight


@contextlib.contextmanager
def _lock(root):
    try:
        import fcntl
    except ImportError as exc:
        raise ExamError("paired scheduling requires POSIX file locks") from exc
    fd = os.open(root / ".execution.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExamError("this schedule is already running") from exc
        yield
    finally:
        os.close(fd)


def run_pair(packet, config, root: Path, *, sources_url=None, resume=False):
    schedule = [
        {"repeat": repeat, "condition": condition}
        for repeat in range(1, config["repeats"] + 1)
        for condition in (("closed-book", "sources") if repeat % 2 else ("sources", "closed-book"))
    ]
    plan = {"schema": "zno-nmt.plan.v1", "packet_sha256": packet["packet_sha256"],
            "config": config, "config_sha256": digest(config), "schedule": schedule}
    if root.is_symlink():
        raise ExamError("schedule directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=resume)
    with _lock(root):
        if resume:
            if read_json(root / "plan.json") != plan:
                raise ExamError("resume inputs differ from frozen schedule")
        else:
            write_private_json(root / "plan.json", plan)
        store = EvidenceStore(root / "evidence")
        existing = store.verify_all()
        slots = {f"r{trial['repeat']:03d}-{trial['condition']}" for trial in schedule}
        if set(existing) - slots:
            raise ExamError("evidence contains an attempt outside the frozen schedule")
        # Check both conditions before any new provider execution.
        if slots - set(existing):
            for condition in ("closed-book", "sources"):
                preflight(config, condition, sources_url)
        failed = False
        for trial in schedule:
            slot = f"r{trial['repeat']:03d}-{trial['condition']}"
            receipt = existing.get(slot)
            if receipt is not None:
                expected = {"denominator": len(packet["items"]), "packet_sha256": packet["packet_sha256"],
                            "config_sha256": digest(config), "condition": trial["condition"]}
                if receipt["metadata"] != expected:
                    raise ExamError("attempt does not match frozen schedule")
                if not receipt["complete"]:
                    # The schedule lock proves no cooperating executor still owns this slot.
                    # Preserve the original events and account for interruption as failure.
                    result = _failure(packet, config, trial["condition"], TimeoutError())
                    result["failure_reason"] = "interrupted"
                    receipt = store.finalize(slot, result, status="interrupted")
                result = receipt["result"]
            else:
                result, receipt = execute_attempt(packet, config, trial["condition"], root / "evidence",
                                                  sources_url=sources_url, attempt_id=slot)
            result = {**result, "repeat": trial["repeat"]}
            for path, payload in [
                (root / f"{trial['repeat']:03d}-{trial['condition']}.json", result),
                (root / f"{trial['repeat']:03d}-{trial['condition']}.evidence.json", receipt),
            ]:
                if path.exists():
                    if read_json(path) != payload:
                        raise ExamError("existing result differs from preserved evidence")
                else:
                    write_private_json(path, payload)
            failed |= result["status"] != "ok"
            yield {**trial, "status": result["status"], "resumed": slot in existing, "failed": failed}
            if result["status"] != "ok" and slot not in existing:
                return
