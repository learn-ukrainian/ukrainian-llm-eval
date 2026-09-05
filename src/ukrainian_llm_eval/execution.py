"""Private evidence-backed execution, separate from offline grading."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core import digest
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Allocate evidence before execution; retain incomplete attempts on interruption.

    An exception from storage is fatal: execution must never silently continue
    without its evidence. No grading key is accepted by this interface.
    """
    store = EvidenceStore(evidence_dir)
    attempt = store.start({
        "denominator": len(packet["items"]),
        "packet_sha256": packet["packet_sha256"],
        "config_sha256": digest(config),
        "condition": condition,
        "route_sha256": route_fingerprint(config, sources_url),
    }, attempt_id=attempt_id)
    result = run_exam(packet, config, condition, sources_url=sources_url, evidence=attempt.append)
    receipt = attempt.finalize(result, status="completed" if result["status"] == "ok" else "failed")
    return result, receipt
