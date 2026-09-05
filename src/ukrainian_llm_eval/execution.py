"""Private evidence-backed execution, separate from offline grading."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core import ExamError, digest
from .evidence import EvidenceStore
from .runner import run_exam


def route_fingerprint(config: Mapping[str, Any], sources_url: str | None) -> str:
    """Bind resolved endpoints without retaining their text or credentials."""
    completion = os.environ.get(str(config.get("endpoint_env"))) if config.get("adapter") == "chat-http" else None
    return digest({"sources": sources_url, "completion": completion})


def execute_attempt(
    packet: Mapping[str, Any],
    config: Mapping[str, Any],
    condition: str,
    evidence_dir: Path,
    *,
    sources_url: str | None = None,
    attempt_id: str | None = None,
    segment_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Allocate evidence before execution; retain incomplete attempts on interruption.

    An exception from storage is fatal: execution must never silently continue
    without its evidence. No grading key is accepted by this interface.
    """
    metadata = {
        "denominator": len(packet["items"]),
        "packet_sha256": packet["packet_sha256"],
        "config_sha256": digest(config),
        "condition": condition,
        "route_sha256": route_fingerprint(config, sources_url),
    }
    if segment_context is not None:
        fields = {"execution_plan_sha256", "cell_id", "segment_id", "reservation_id", "reserved_micro_usd"}
        if not isinstance(segment_context, Mapping) or set(segment_context) != fields:
            raise ExamError("invalid segment execution context")
        if not isinstance(segment_context["execution_plan_sha256"], str) or re.fullmatch(
            r"[0-9a-f]{64}", segment_context["execution_plan_sha256"]
        ) is None:
            raise ExamError("invalid execution plan digest")
        for field in ("cell_id", "segment_id", "reservation_id"):
            if not isinstance(segment_context[field], str) or re.fullmatch(
                r"[a-z0-9][a-z0-9_-]{0,63}", segment_context[field]
            ) is None:
                raise ExamError("invalid segment execution identifier")
        if type(segment_context["reserved_micro_usd"]) is not int or segment_context["reserved_micro_usd"] < 0:
            raise ExamError("invalid segment reservation")
        metadata["segment_context"] = dict(segment_context)
    store = EvidenceStore(evidence_dir)
    attempt = store.start(metadata, attempt_id=attempt_id)
    result = run_exam(packet, config, condition, sources_url=sources_url, evidence=attempt.append)
    receipt = attempt.finalize(result, status="completed" if result["status"] == "ok" else "failed")
    return result, receipt
