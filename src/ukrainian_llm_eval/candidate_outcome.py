"""Classification helpers for preserved candidate-task failures.

The runner keeps the versioned run schema intentionally small.  A response
that fails only the answer payload contract therefore remains a normal failed
run, with a stable failure reason and the independently verified native
identity/metrics retained.  Transport, envelope, identity, and tool-policy
failures continue to use the ordinary fail-closed path.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

CANDIDATE_RESPONSE_ERROR = "candidate_response_error"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def is_candidate_response_failure(
    result: Mapping[str, Any] | Any,
    *,
    expected_response_ids: Collection[str] | None = None,
) -> bool:
    """Return whether *result* is an envelope-verified answer-payload failure.

    This predicate is deliberately strict enough for scheduler continuation:
    only a failed result with the adapter's stable task-only reason, native
    identity fields, object-shaped metrics with an observed tool count, and an
    all-null response map is eligible.  Session reuse is checked by the
    scheduler across attempts. Missing or malformed evidence returns ``False``
    so callers retain fail-closed behaviour.
    """

    if not isinstance(result, Mapping):
        return False
    if result.get("status") != "failed" or result.get("failure_reason") != CANDIDATE_RESPONSE_ERROR:
        return False
    responses = result.get("responses")
    identity = result.get("identity")
    metrics = result.get("metrics")
    if not isinstance(responses, Mapping) or not isinstance(identity, Mapping) or not isinstance(metrics, Mapping):
        return False
    required_identity = (
        ("adapter", "kimi"),
        ("harness", "kimi-cli"),
        ("provider", "managed:kimi-code"),
    )
    if any(identity.get(field) != expected for field, expected in required_identity):
        return False
    for field in ("model", "requested_model", "requested_model_alias", "cli_version"):
        value = identity.get(field)
        if not isinstance(value, str) or not value or value.strip() != value:
            return False
    for field in ("binary_sha256", "native_config_sha256", "catalog_provider_sha256", "catalog_model_sha256"):
        value = identity.get(field)
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            return False
    session_id = identity.get("session_id")
    if not isinstance(session_id, str) or not session_id or session_id.strip() != session_id:
        return False
    tool_calls = metrics.get("tool_calls")
    if isinstance(tool_calls, bool) or not isinstance(tool_calls, int) or tool_calls < 0:
        return False
    if any(not isinstance(item_id, str) for item_id in responses):
        return False
    if any(value is not None for value in responses.values()):
        return False
    if expected_response_ids is not None:
        if any(not isinstance(item_id, str) for item_id in expected_response_ids):
            return False
        if set(responses) != set(expected_response_ids):
            return False
    return True


__all__ = ["CANDIDATE_RESPONSE_ERROR", "is_candidate_response_failure"]
