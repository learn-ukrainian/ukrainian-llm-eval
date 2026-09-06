"""Verify prepared benchmark artifacts against a caller supplied profile.

The profile is a declaration supplied by the caller.  This module binds the
raw source, reconstruction, prepared packet and key to that declaration; it
does not decide whether a source is official or whether execution is approved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from .core import (
    ExamError,
    _duplicate_rejecting_pairs,
    _reject_json_constant,
    digest,
    prepare_exam,
)
from .gec import _is_document_heading, _parse_m2, prepare_gec
from .importers import import_zno
from .typography import apply_typography
from .ulp import import_ulp

MANIFEST_SCHEMA = "ukrainian-llm-eval.benchmark-manifest.v1"
EXPERIMENT_MANIFEST_SCHEMA = "ukrainian-llm-eval.experiment-manifest.v1"
EXECUTION_PLAN_SCHEMA = "ukrainian-llm-eval.execution-plan.v1"
EXPERIMENT_MANIFEST_SEQUENTIAL_SCHEMA = "ukrainian-llm-eval.experiment-manifest.v2"
EXECUTION_PLAN_SEQUENTIAL_SCHEMA = "ukrainian-llm-eval.execution-plan.v2"
SPENDING_POLICY_SCHEMA = "ukrainian-llm-eval.spending-policy.v1"
USAGE_BOUND_SPENDING_POLICY_SCHEMA = "ukrainian-llm-eval.spending-policy.v2"
DEFAULT_NEW_SPEND_CAP_MICRO_USD = 10_000_000

# Costs are represented as integer micro-dollars.  Token rates are per million
# tokens because a per-token price is commonly below one micro-dollar.
_MILLION_TOKENS = 1_000_000
_CONDITIONS = ("closed-book", "sources")
_IDENTIFIER_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")

_SUPPORTED_SUITES = frozenset({
    "nmt-2022-demo-ukrainian",
    "ulp",
    "ua-gec-public-gec-only-test",
})
_COMMON_PROFILE_FIELDS = {"id", "revision", "source_sha256", "license", "denominator"}
_NMT_DENOMINATOR_FIELDS = {"items", "single_choice", "matching", "points"}
_ULP_DENOMINATOR_FIELDS = {"items", "points"}
_GEC_DENOMINATOR_FIELDS = {"sentences", "documents", "tokens"}
_PROVENANCE_FIELDS = {"source_url", "source_revision", "license", "exposure"}
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def _require_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExamError(f"{where} must be an object")
    return value


def _require_exact_dict(value: Any, expected: set[str], where: str) -> dict[str, Any]:
    result = _require_dict(value, where)
    if set(result) != expected:
        missing = sorted(expected - set(result), key=repr)
        extra = sorted(set(result) - expected, key=repr)
        detail: list[str] = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unknown {extra}")
        raise ExamError(f"{where} has invalid fields ({'; '.join(detail)})")
    return result


def _require_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExamError(f"{where} must be a non-empty string")
    return value


def _require_digest(value: Any, where: str) -> str:
    text = _require_string(value, where)
    if _DIGEST_RE.fullmatch(text) is None:
        raise ExamError(f"{where} must be a lowercase SHA-256 hex digest")
    return text


def _require_count(value: Any, where: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or (value < 0 if allow_zero else value <= 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ExamError(f"{where} must be a {qualifier} integer")
    return value


def _profile_denominator(profile: dict[str, Any], suite_id: str) -> dict[str, int]:
    denominator = profile["denominator"]
    if suite_id == "nmt-2022-demo-ukrainian":
        expected_fields = _NMT_DENOMINATOR_FIELDS
    elif suite_id == "ulp":
        expected_fields = _ULP_DENOMINATOR_FIELDS
    else:
        expected_fields = _GEC_DENOMINATOR_FIELDS
    if suite_id == "ua-gec-public-gec-only-test":
        result = _require_dict(denominator, "profile.denominator")
        if "sentences" not in result:
            raise ExamError("profile.denominator is missing fields ['sentences']")
        unknown = sorted(set(result) - expected_fields, key=repr)
        if unknown:
            raise ExamError(f"profile.denominator has unknown fields {unknown}")
    else:
        result = _require_exact_dict(denominator, expected_fields, "profile.denominator")
    for field, value in result.items():
        _require_count(
            value,
            f"profile.denominator.{field}",
            allow_zero=field in {"single_choice", "matching", "documents"},
        )
    if suite_id == "nmt-2022-demo-ukrainian":
        if result["single_choice"] + result["matching"] != result["items"]:
            raise ExamError("profile.denominator item kind counts do not add up")
        if result["points"] < result["items"]:
            raise ExamError("profile.denominator.points cannot be below item count")
    elif suite_id == "ulp" and result["points"] != result["items"]:
        raise ExamError("profile.denominator points must equal items for ULP")
    return result


def _validate_profile(profile: Any) -> tuple[dict[str, Any], str, dict[str, int]]:
    value = _require_dict(profile, "profile")
    missing = sorted(_COMMON_PROFILE_FIELDS - set(value))
    if missing:
        raise ExamError(f"profile is missing fields {missing}")
    suite_id = _require_string(value["id"], "profile.id")
    if suite_id not in _SUPPORTED_SUITES:
        raise ExamError(f"unsupported benchmark suite: {suite_id!r}")
    _require_string(value["revision"], "profile.revision")
    _require_digest(value["source_sha256"], "profile.source_sha256")
    _require_string(value["license"], "profile.license")
    denominator = _profile_denominator(value, suite_id)
    if suite_id == "nmt-2022-demo-ukrainian":
        selection = _require_exact_dict(value.get("selection"), {"test_id"}, "profile.selection")
        _require_string(selection["test_id"], "profile.selection.test_id")
        official = _require_exact_dict(value.get("official_paper"), {"url", "sha256"}, "profile.official_paper")
        _require_string(official["url"], "profile.official_paper.url")
        _require_digest(official["sha256"], "profile.official_paper.sha256")
    return value, suite_id, denominator


def _decode_utf8(source_bytes: bytes) -> str:
    try:
        return source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExamError("benchmark source must be valid UTF-8") from exc


def _parse_json(source_text: str) -> Any:
    try:
        return json.loads(
            source_text,
            object_pairs_hook=_duplicate_rejecting_pairs,
            parse_constant=_reject_json_constant,
        )
    except ExamError:
        raise
    except json.JSONDecodeError as exc:
        raise ExamError(f"benchmark source is not valid JSON: {exc}") from exc


def _parse_jsonl(source_text: str) -> list[Any]:
    lines = source_text.splitlines()
    if not lines:
        raise ExamError("ULP source must contain JSONL rows")
    rows: list[Any] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            raise ExamError(f"ULP source line {index} is blank")
        try:
            rows.append(
                json.loads(
                    line,
                    object_pairs_hook=_duplicate_rejecting_pairs,
                    parse_constant=_reject_json_constant,
                )
            )
        except ExamError:
            raise
        except json.JSONDecodeError as exc:
            raise ExamError(f"ULP source line {index} is not valid JSON: {exc}") from exc
    return rows


def _validate_provenance(provenance: Any, profile: dict[str, Any], where: str) -> None:
    value = _require_exact_dict(provenance, _PROVENANCE_FIELDS, where)
    _require_string(value["source_url"], f"{where}.source_url")
    source_revision = _require_string(value["source_revision"], f"{where}.source_revision")
    license_name = _require_string(value["license"], f"{where}.license")
    _require_string(value["exposure"], f"{where}.exposure")
    if source_revision != profile["revision"]:
        raise ExamError(f"{where}.source_revision does not match profile revision")
    if license_name != profile["license"]:
        raise ExamError(f"{where}.license does not match profile license")


def _validate_source_hash(profile: dict[str, Any], source_bytes: bytes) -> str:
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != profile["source_sha256"]:
        raise ExamError("benchmark source hash does not match profile")
    return source_sha256


def _validate_mcq_denominator(exam: dict[str, Any], denominator: dict[str, int], suite_id: str) -> None:
    items = exam["items"]
    observed = {
        "items": len(items),
        "single_choice": sum(item["kind"] == "single" for item in items),
        "matching": sum(item["kind"] == "matching" for item in items),
        "points": sum(1 if item["kind"] == "single" else len(item["rows"]) for item in items),
    }
    for field, expected in denominator.items():
        if observed[field] != expected:
            raise ExamError(f"{suite_id} denominator mismatch for {field}")


def _metadata_from_exam(exam: dict[str, Any]) -> dict[str, Any]:
    return {field: copy.deepcopy(exam[field]) for field in ("title", "subject", "year", "provenance", "scoring")}


def _verify_mcq(
    profile: dict[str, Any],
    suite_id: str,
    denominator: dict[str, int],
    source_text: str,
    packet: Any,
    key: Any,
    exam: Any,
    overlay: Any,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    if not isinstance(exam, dict):
        raise ExamError("MCQ verification requires an exam")
    if not isinstance(packet, dict) or not isinstance(key, dict):
        raise ExamError("MCQ verification requires packet and key objects")
    prepare_exam(copy.deepcopy(exam))
    _validate_provenance(exam["provenance"], profile, "exam.provenance")
    metadata = _metadata_from_exam(exam)
    if suite_id == "nmt-2022-demo-ukrainian":
        selection = profile["selection"]
        source_value = _parse_json(source_text)
        raw_exam = import_zno(source_value, selection["test_id"], metadata)
        if overlay is None:
            raise ExamError("NMT verification requires a typography overlay")
        if not isinstance(overlay, dict) or overlay.get("official_source") != profile["official_paper"]:
            raise ExamError("typography overlay official source does not match profile")
        reconstructed_exam, typography_receipt = apply_typography(raw_exam, overlay)
        overlay_sha256 = typography_receipt["overlay_sha256"]
    else:
        if overlay is not None:
            raise ExamError("ULP does not accept a typography overlay")
        rows = _parse_jsonl(source_text)
        reconstructed_exam = import_ulp(rows, metadata)
        overlay_sha256 = None

    if reconstructed_exam != exam:
        raise ExamError("reconstructed exam does not match supplied exam")
    _validate_mcq_denominator(reconstructed_exam, denominator, suite_id)
    expected_packet, expected_key = prepare_exam(copy.deepcopy(reconstructed_exam))
    if packet != expected_packet:
        raise ExamError("supplied packet does not match reconstructed exam")
    if key != expected_key:
        raise ExamError("supplied key does not match reconstructed exam")
    return reconstructed_exam, expected_packet, overlay_sha256


def _gec_observed_counts(source_text: str, packet: dict[str, Any]) -> dict[str, int]:
    blocks = _parse_m2(source_text)
    content_blocks = [block for block in blocks if not _is_document_heading(block)]
    return {
        "sentences": len(packet["items"]),
        "documents": sum(_is_document_heading(block) for block in blocks),
        "tokens": sum(len(block["text"].split()) for block in content_blocks),
    }


def _verify_gec(
    profile: dict[str, Any],
    denominator: dict[str, int],
    source_text: str,
    packet: Any,
    key: Any,
    exam: Any,
    overlay: Any,
) -> tuple[None, dict[str, Any], str | None]:
    if exam is not None:
        raise ExamError("GEC verification does not accept an exam")
    if overlay is not None:
        raise ExamError("GEC verification does not accept a typography overlay")
    if not isinstance(key, dict):
        raise ExamError("GEC verification requires a key object")
    _validate_provenance(key.get("provenance"), profile, "GEC key.provenance")
    reconstructed_packet, reconstructed_key = prepare_gec(
        source_text,
        key["provenance"],
        expected_sentences=denominator.get("sentences"),
        expected_documents=denominator.get("documents"),
    )
    if packet != reconstructed_packet:
        raise ExamError("supplied packet does not match reconstructed GEC source")
    if key != reconstructed_key:
        raise ExamError("supplied key does not match reconstructed GEC source")
    observed = _gec_observed_counts(source_text, reconstructed_packet)
    for field, expected in denominator.items():
        if observed[field] != expected:
            raise ExamError(f"{profile['id']} denominator mismatch for {field}")
    return None, reconstructed_packet, None


def verify_benchmark(
    profile: dict[str, Any],
    source_bytes: bytes,
    packet: dict[str, Any],
    key: dict[str, Any],
    *,
    exam: dict[str, Any] | None = None,
    overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a compact manifest after exact profile-bound reconstruction.

    ``profile`` is caller supplied and its digest is included in the result;
    this function makes no independent claim that the profile or its official
    source declaration is trustworthy.  MCQ packets require their source exam
    and GEC packets intentionally do not have one.
    """

    if type(source_bytes) is not bytes:
        raise ExamError("benchmark source must be bytes")
    checked_profile, suite_id, denominator = _validate_profile(profile)
    profile_sha256 = digest(checked_profile)
    source_sha256 = _validate_source_hash(checked_profile, source_bytes)
    source_text = _decode_utf8(source_bytes)

    if suite_id == "ua-gec-public-gec-only-test":
        checked_exam, checked_packet, overlay_sha256 = _verify_gec(
            checked_profile, denominator, source_text, packet, key, exam, overlay
        )
    else:
        checked_exam, checked_packet, overlay_sha256 = _verify_mcq(
            checked_profile, suite_id, denominator, source_text, packet, key, exam, overlay
        )

    return {
        "schema": MANIFEST_SCHEMA,
        "suite_id": suite_id,
        "verification": "matches_supplied_profile",
        "profile_sha256": profile_sha256,
        "source_sha256": source_sha256,
        "packet_sha256": checked_packet["packet_sha256"],
        "key_sha256": digest(key),
        "exam_sha256": None if checked_exam is None else digest(checked_exam),
        "overlay_sha256": overlay_sha256,
        "denominator": copy.deepcopy(denominator),
        "license": checked_profile["license"],
        "source_revision": checked_profile["revision"],
    }


def _require_identifier(value: Any, where: str) -> str:
    text = _require_string(value, where)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise ExamError(f"{where} must be a lower-case identifier of at most 64 characters")
    return text


def _require_nonnegative_integer(value: Any, where: str) -> int:
    if type(value) is not int or value < 0:
        raise ExamError(f"{where} must be a non-negative integer")
    return value


def _require_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ExamError(f"{where} must be an array")
    return value


def _require_unique_identifiers(values: list[Any], where: str) -> list[str]:
    result = [_require_identifier(value, f"{where}[{index}]") for index, value in enumerate(values)]
    if len(set(result)) != len(result):
        raise ExamError(f"{where} contains duplicate identifiers")
    return result


def _normalise_segment_plan(value: Any, suite_id: str) -> dict[str, Any]:
    """Validate the segmentation owner's complete frozen plan without replacing it.

    This module deliberately does not duplicate segment-plan construction.  It
    needs just enough structural information to enumerate immutable segment
    reservations, while the segmentation module remains the owner of packet
    and denominator semantics.
    """

    fields = {
        "schema", "protocol_sha256", "suite_id", "source_packet_sha256", "source_sha256", "denominator",
        "unit", "segments", "segment_plan_sha256",
    }
    plan = _require_exact_dict(value, fields, "suite.segment_plan")
    _require_string(plan["schema"], "suite.segment_plan.schema")
    if _require_identifier(plan["suite_id"], "suite.segment_plan.suite_id") != suite_id:
        raise ExamError("suite.segment_plan.suite_id does not match suite_id")
    _require_digest(plan["protocol_sha256"], "suite.segment_plan.protocol_sha256")
    _require_digest(plan["source_packet_sha256"], "suite.segment_plan.source_packet_sha256")
    if plan["source_sha256"] is not None:
        _require_digest(plan["source_sha256"], "suite.segment_plan.source_sha256")
    _require_dict(plan["denominator"], "suite.segment_plan.denominator")
    _require_identifier(plan["unit"], "suite.segment_plan.unit")
    segments = _require_list(plan["segments"], "suite.segment_plan.segments")
    if not segments:
        raise ExamError("suite.segment_plan.segments must not be empty")
    normalized_segments: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_items: set[str] = set()
    for index, segment in enumerate(segments):
        item = _require_exact_dict(segment, {"segment_id", "item_ids", "packet_sha256"},
                                   f"suite.segment_plan.segments[{index}]")
        segment_id = _require_identifier(item["segment_id"], f"suite.segment_plan.segments[{index}].segment_id")
        if segment_id in seen:
            raise ExamError("suite.segment_plan.segments contains duplicate segment_id")
        seen.add(segment_id)
        item_ids = _require_unique_identifiers(
            _require_list(item["item_ids"], f"suite.segment_plan.segments[{index}].item_ids"),
            f"suite.segment_plan.segments[{index}].item_ids",
        )
        if not item_ids:
            raise ExamError("suite.segment_plan segment must contain at least one item")
        if seen_items.intersection(item_ids):
            raise ExamError("suite.segment_plan.segments contains duplicate item_ids")
        seen_items.update(item_ids)
        normalized_segments.append({
            "segment_id": segment_id,
            "item_ids": item_ids,
            "packet_sha256": _require_digest(
                item["packet_sha256"], f"suite.segment_plan.segments[{index}].packet_sha256"
            ),
        })
    supplied_hash = _require_digest(plan["segment_plan_sha256"], "suite.segment_plan.segment_plan_sha256")
    normalized = {
        "schema": _require_string(plan["schema"], "suite.segment_plan.schema"),
        "protocol_sha256": plan["protocol_sha256"],
        "suite_id": suite_id,
        "source_packet_sha256": plan["source_packet_sha256"],
        "source_sha256": plan["source_sha256"],
        "denominator": copy.deepcopy(plan["denominator"]),
        "unit": plan["unit"],
        "segments": normalized_segments,
    }
    if digest(normalized) != supplied_hash:
        raise ExamError("suite.segment_plan.segment_plan_sha256 does not match its frozen body")
    return normalized | {"segment_plan_sha256": supplied_hash}


def _normalise_suite(value: Any) -> dict[str, Any]:
    suite = _require_exact_dict(value, {"suite_id", "source_sha256", "profile_sha256", "key_sha256", "segment_plan", "limits"},
                                "suite")
    suite_id = _require_identifier(suite["suite_id"], "suite.suite_id")
    if suite_id not in _SUPPORTED_SUITES:
        raise ExamError(f"unsupported benchmark suite: {suite_id!r}")
    source_sha256 = _require_digest(suite["source_sha256"], "suite.source_sha256")
    profile_sha256 = _require_digest(suite["profile_sha256"], "suite.profile_sha256")
    key_sha256 = _require_digest(suite["key_sha256"], "suite.key_sha256")
    limits = _require_exact_dict(suite["limits"], {"timeout_seconds", "max_output_tokens", "max_tool_calls"},
                                 "suite.limits")
    normalized_limits = {
        "timeout_seconds": _require_count(limits["timeout_seconds"], "suite.limits.timeout_seconds"),
        "max_output_tokens": _require_count(limits["max_output_tokens"], "suite.limits.max_output_tokens"),
        "max_tool_calls": _require_nonnegative_integer(limits["max_tool_calls"], "suite.limits.max_tool_calls"),
    }
    segment_plan = _normalise_segment_plan(suite["segment_plan"], suite_id)
    # MCQ plans intentionally retain no raw source hash; GEC plans do.  Where
    # the plan has one, it must bind the same source receipt as this suite.
    if segment_plan["source_sha256"] is not None and segment_plan["source_sha256"] != source_sha256:
        raise ExamError("suite.segment_plan.source_sha256 does not match suite source")
    return {
        "suite_id": suite_id,
        "source_sha256": source_sha256,
        "profile_sha256": profile_sha256,
        "key_sha256": key_sha256,
        "segment_plan": segment_plan,
        "limits": normalized_limits,
    }


def _normalise_billing(value: Any, where: str) -> dict[str, Any]:
    fields = {
        "kind", "units", "input_micro_usd_per_million_tokens", "output_micro_usd_per_million_tokens",
        "max_total_input_tokens", "max_total_output_tokens", "max_tool_rounds", "tool_round_micro_usd",
    }
    billing = _require_exact_dict(value, fields, where)
    kind = _require_string(billing["kind"], f"{where}.kind")
    if kind not in {"metered", "verified_subscription", "existing_credit"}:
        raise ExamError(f"{where}.kind is not supported")
    if billing["units"] != "tokens":
        raise ExamError(f"{where}.units must be 'tokens'")
    normalized = {
        "kind": kind,
        "units": "tokens",
        "input_micro_usd_per_million_tokens": _require_nonnegative_integer(
            billing["input_micro_usd_per_million_tokens"], f"{where}.input_micro_usd_per_million_tokens"
        ),
        "output_micro_usd_per_million_tokens": _require_nonnegative_integer(
            billing["output_micro_usd_per_million_tokens"], f"{where}.output_micro_usd_per_million_tokens"
        ),
        "max_total_input_tokens": _require_count(billing["max_total_input_tokens"], f"{where}.max_total_input_tokens"),
        "max_total_output_tokens": _require_count(billing["max_total_output_tokens"], f"{where}.max_total_output_tokens"),
        "max_tool_rounds": _require_nonnegative_integer(billing["max_tool_rounds"], f"{where}.max_tool_rounds"),
        "tool_round_micro_usd": _require_nonnegative_integer(billing["tool_round_micro_usd"], f"{where}.tool_round_micro_usd"),
    }
    if kind == "metered" and not any(
        normalized[field]
        for field in (
            "input_micro_usd_per_million_tokens", "output_micro_usd_per_million_tokens", "tool_round_micro_usd",
        )
    ):
        raise ExamError(f"{where} has unknown or zero-only metered pricing; use a receipt-bound entitlement")
    if kind == "verified_subscription" and any(
        normalized[field]
        for field in (
            "input_micro_usd_per_million_tokens", "output_micro_usd_per_million_tokens", "tool_round_micro_usd",
        )
    ):
        raise ExamError(f"{where} zero-incremental entitlement must not carry metered rates")
    return normalized


def _normalise_route(value: Any) -> dict[str, Any]:
    fields = {
        "route_id", "route_sha256", "config_sha256", "conditions", "unsupported_condition_evidence",
        "capability_evidence_sha256", "pricing_evidence_sha256", "entitlement_evidence_sha256", "billing",
        "admission_command_sha256", "operator_authorization_sha256", "request_budget_mechanism_sha256",
    }
    route = _require_exact_dict(value, fields, "route")
    conditions = _require_unique_identifiers(_require_list(route["conditions"], "route.conditions"), "route.conditions")
    if not conditions or any(condition not in _CONDITIONS for condition in conditions):
        raise ExamError("route.conditions must be a non-empty subset of the frozen conditions")
    unsupported = _require_dict(route["unsupported_condition_evidence"], "route.unsupported_condition_evidence")
    expected_unsupported = set(_CONDITIONS) - set(conditions)
    if set(unsupported) != expected_unsupported:
        raise ExamError("route.unsupported_condition_evidence must explain exactly the unavailable conditions")
    normalized_unsupported = {
        condition: _require_digest(unsupported[condition], f"route.unsupported_condition_evidence.{condition}")
        for condition in sorted(unsupported)
    }
    billing = _normalise_billing(route["billing"], "route.billing")
    request_budget = route["request_budget_mechanism_sha256"]
    if billing["kind"] in {"metered", "existing_credit"} or request_budget is not None:
        _require_digest(request_budget, "route.request_budget_mechanism_sha256")
    entitlement = route["entitlement_evidence_sha256"]
    _require_digest(entitlement, "route.entitlement_evidence_sha256")
    return {
        "route_id": _require_identifier(route["route_id"], "route.route_id"),
        "route_sha256": _require_digest(route["route_sha256"], "route.route_sha256"),
        "config_sha256": _require_digest(route["config_sha256"], "route.config_sha256"),
        "conditions": sorted(conditions),
        "unsupported_condition_evidence": normalized_unsupported,
        "capability_evidence_sha256": _require_digest(route["capability_evidence_sha256"], "route.capability_evidence_sha256"),
        "pricing_evidence_sha256": _require_digest(route["pricing_evidence_sha256"], "route.pricing_evidence_sha256"),
        "entitlement_evidence_sha256": entitlement,
        "admission_command_sha256": _require_digest(route["admission_command_sha256"], "route.admission_command_sha256"),
        "operator_authorization_sha256": _require_digest(route["operator_authorization_sha256"], "route.operator_authorization_sha256"),
        "request_budget_mechanism_sha256": request_budget,
        "billing": billing,
    }


def _validate_route_capacity(route: dict[str, Any], suite: dict[str, Any]) -> None:
    billing = route["billing"]
    limits = suite["limits"]
    if billing["max_tool_rounds"] < limits["max_tool_calls"]:
        raise ExamError("route billing max_tool_rounds is below the suite segment tool limit")
    required_outputs = limits["max_output_tokens"]
    if "sources" in route["conditions"]:
        required_outputs *= limits["max_tool_calls"] + 1
    if billing["max_total_output_tokens"] < required_outputs:
        raise ExamError("route billing max_total_output_tokens is below the conservative segment output bound")


def _ceil_rate_cost(rate_micro_per_million: int, tokens: int) -> int:
    return (rate_micro_per_million * tokens + _MILLION_TOKENS - 1) // _MILLION_TOKENS


def _reservation_micro_usd(route: dict[str, Any]) -> int:
    billing = route["billing"]
    if billing["kind"] != "metered":
        return 0
    return (
        _ceil_rate_cost(billing["input_micro_usd_per_million_tokens"], billing["max_total_input_tokens"])
        + _ceil_rate_cost(billing["output_micro_usd_per_million_tokens"], billing["max_total_output_tokens"])
        + billing["max_tool_rounds"] * billing["tool_round_micro_usd"]
    )


def _config_binding_sha256(base_config_sha256: str, limits: dict[str, int], repeats: int) -> str:
    """Bind the scheduler's explicit derived segment config without inventing a config body."""

    return digest({"base_config_sha256": base_config_sha256, "limits": limits, "repeats": repeats})


def _normalise_experiment_inputs(
    protocol_sha256: Any, suites: Any, routes: Any, scorer_sha256: Any, tool_policy_sha256: Any,
    repeats: Any, new_spend_cap_micro_usd: Any,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], str, str, int, int]:
    checked_protocol = _require_digest(protocol_sha256, "protocol_sha256")
    checked_suites = [_normalise_suite(suite) for suite in _require_list(suites, "suites")]
    if not checked_suites:
        raise ExamError("suites must not be empty")
    if len({suite["suite_id"] for suite in checked_suites}) != len(checked_suites):
        raise ExamError("suites contains duplicate suite_id")
    checked_suites.sort(key=lambda suite: suite["suite_id"])
    for suite in checked_suites:
        if suite["segment_plan"]["protocol_sha256"] != checked_protocol:
            raise ExamError("suite.segment_plan.protocol_sha256 does not match experiment protocol")
    checked_routes = [_normalise_route(route) for route in _require_list(routes, "routes")]
    if not checked_routes:
        raise ExamError("routes must not be empty")
    if len({route["route_id"] for route in checked_routes}) != len(checked_routes):
        raise ExamError("routes contains duplicate route_id")
    checked_routes.sort(key=lambda route: route["route_id"])
    for route in checked_routes:
        for suite in checked_suites:
            _validate_route_capacity(route, suite)
    return (
        checked_protocol,
        checked_suites,
        checked_routes,
        _require_digest(scorer_sha256, "scorer_sha256"),
        _require_digest(tool_policy_sha256, "tool_policy_sha256"),
        _require_count(repeats, "repeats"),
        _require_nonnegative_integer(new_spend_cap_micro_usd, "new_spend_cap_micro_usd"),
    )


def _normalise_spending_policy(value: Any, cap_micro_usd: int) -> dict[str, Any] | None:
    if value is None:
        return None
    fields = {
        "schema", "mode", "ledger_id", "authorized_cap_micro_usd", "reservation_scope",
        "settlement", "cap_stop",
    }
    policy = _require_exact_dict(value, fields, "spending_policy")
    settlements = {
        SPENDING_POLICY_SCHEMA: "authoritative_final_account_charge_only",
        USAGE_BOUND_SPENDING_POLICY_SCHEMA: (
            "authoritative_account_charge_or_conservative_final_usage_upper_bound"
        ),
    }
    schema = policy["schema"]
    if schema not in settlements:
        raise ExamError("unsupported spending policy schema")
    if policy["mode"] != "sequential_shared_cap":
        raise ExamError("unsupported spending policy mode")
    ledger_id = _require_identifier(policy["ledger_id"], "spending_policy.ledger_id")
    authorized_cap = _require_nonnegative_integer(
        policy["authorized_cap_micro_usd"], "spending_policy.authorized_cap_micro_usd"
    )
    if authorized_cap != cap_micro_usd:
        raise ExamError("spending policy cap differs from experiment new-spend cap")
    if policy["reservation_scope"] != "whole_segment_before_first_request":
        raise ExamError("unsupported spending reservation scope")
    settlement = settlements[schema]
    if policy["settlement"] != settlement:
        raise ExamError("unsupported spending settlement policy")
    if policy["cap_stop"] != "not_executed_budget":
        raise ExamError("unsupported spending cap-stop policy")
    return {
        "schema": schema,
        "mode": "sequential_shared_cap",
        "ledger_id": ledger_id,
        "authorized_cap_micro_usd": authorized_cap,
        "reservation_scope": "whole_segment_before_first_request",
        "settlement": settlement,
        "cap_stop": "not_executed_budget",
    }


def build_experiment_manifest(
    protocol_sha256: str,
    suites: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    *,
    scorer_sha256: str,
    tool_policy_sha256: str,
    repeats: int = 3,
    new_spend_cap_micro_usd: int = DEFAULT_NEW_SPEND_CAP_MICRO_USD,
    spending_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze a protocol-bound experiment description without admitting paid execution.

    Evidence fields are only receipt bindings.  This pure function deliberately
    records them as ``bound_not_verified``; a route preflight must authenticate
    and validate the receipt before any provider request is allowed.
    """

    protocol, checked_suites, checked_routes, scorer, tool_policy, count, cap = _normalise_experiment_inputs(
        protocol_sha256, suites, routes, scorer_sha256, tool_policy_sha256, repeats, new_spend_cap_micro_usd
    )
    checked_policy = _normalise_spending_policy(spending_policy, cap)
    body = {
        "schema": EXPERIMENT_MANIFEST_SCHEMA if checked_policy is None else EXPERIMENT_MANIFEST_SEQUENTIAL_SCHEMA,
        "protocol_sha256": protocol,
        "suites": checked_suites,
        "routes": checked_routes,
        "scorer_sha256": scorer,
        "tool_policy_sha256": tool_policy,
        "repeats": count,
        "new_spend_cap_micro_usd": cap,
        "evidence_status": "bound_not_verified",
        "execution_admission": "requires_authoritative_runtime_preflight",
    }
    if checked_policy is not None:
        body["spending_policy"] = checked_policy
    return body | {"experiment_manifest_sha256": digest(body)}


def validate_experiment_manifest(manifest: Any) -> dict[str, Any]:
    """Validate a frozen experiment artifact and return an isolated copy."""

    base_fields = {
        "schema", "protocol_sha256", "suites", "routes", "scorer_sha256", "tool_policy_sha256", "repeats",
        "new_spend_cap_micro_usd", "evidence_status", "execution_admission", "experiment_manifest_sha256",
    }
    if not isinstance(manifest, dict):
        raise ExamError("experiment manifest must be an object")
    schema = manifest.get("schema")
    if schema == EXPERIMENT_MANIFEST_SCHEMA:
        fields = base_fields
    elif schema == EXPERIMENT_MANIFEST_SEQUENTIAL_SCHEMA:
        fields = base_fields | {"spending_policy"}
    else:
        raise ExamError("unsupported experiment manifest schema")
    checked = _require_exact_dict(manifest, fields, "experiment manifest")
    if checked["evidence_status"] != "bound_not_verified":
        raise ExamError("experiment manifest must not claim evidence verification")
    if checked["execution_admission"] != "requires_authoritative_runtime_preflight":
        raise ExamError("experiment manifest must require authoritative runtime preflight")
    rebuilt = build_experiment_manifest(
        checked["protocol_sha256"], checked["suites"], checked["routes"],
        scorer_sha256=checked["scorer_sha256"], tool_policy_sha256=checked["tool_policy_sha256"],
        repeats=checked["repeats"], new_spend_cap_micro_usd=checked["new_spend_cap_micro_usd"],
        spending_policy=checked.get("spending_policy"),
    )
    if rebuilt != checked:
        raise ExamError("experiment manifest does not match its canonical frozen construction")
    return copy.deepcopy(rebuilt)


def _condition_order(route: dict[str, Any], repeat: int) -> list[str]:
    available = set(route["conditions"])
    paired = ("closed-book", "sources") if repeat % 2 else ("sources", "closed-book")
    return [condition for condition in paired if condition in available]


def _build_execution_plan(manifest: dict[str, Any]) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    sequential = manifest["schema"] == EXPERIMENT_MANIFEST_SEQUENTIAL_SCHEMA
    for suite in manifest["suites"]:
        for route in manifest["routes"]:
            reservation = _reservation_micro_usd(route)
            config_binding = _config_binding_sha256(route["config_sha256"], suite["limits"], manifest["repeats"])
            for repeat in range(1, manifest["repeats"] + 1):
                for condition in _condition_order(route, repeat):
                    cell_id = "cell-" + digest({
                        "suite_id": suite["suite_id"], "route_id": route["route_id"],
                        "repeat": repeat, "condition": condition,
                    })[:48]
                    segments = []
                    for segment in suite["segment_plan"]["segments"]:
                        segment_id = segment["segment_id"]
                        reservation_identity = {"cell_id": cell_id, "segment_id": segment_id}
                        if sequential:
                            reservation_identity["experiment_manifest_sha256"] = manifest["experiment_manifest_sha256"]
                        segments.append({
                            "segment_id": segment_id,
                            "segment_packet_sha256": segment["packet_sha256"],
                            "attempt_id": "attempt-" + digest({"cell_id": cell_id, "segment_id": segment_id})[:48],
                            "reservation_id": "reserve-" + digest(reservation_identity)[:48],
                            "reserved_micro_usd": reservation,
                        })
                    cells.append({
                        "cell_id": cell_id,
                        "suite_id": suite["suite_id"],
                        "route_id": route["route_id"],
                        "repeat": repeat,
                        "condition": condition,
                        "route_sha256": route["route_sha256"],
                        "base_config_sha256": route["config_sha256"],
                        "derived_config_sha256": config_binding,
                        "segment_plan_sha256": suite["segment_plan"]["segment_plan_sha256"],
                        "segments": segments,
                    })
    total = sum(segment["reserved_micro_usd"] for cell in cells for segment in cell["segments"])
    if not sequential and total > manifest["new_spend_cap_micro_usd"]:
        raise ExamError("conservative segment reservations exceed the experiment new-spend cap")
    body = {
        "schema": EXECUTION_PLAN_SEQUENTIAL_SCHEMA if sequential else EXECUTION_PLAN_SCHEMA,
        "experiment_manifest_sha256": manifest["experiment_manifest_sha256"],
        "new_spend_cap_micro_usd": manifest["new_spend_cap_micro_usd"],
        "reservation_total_micro_usd": total,
        "cells": cells,
        "execution_admission": "requires_authoritative_runtime_preflight",
    }
    if sequential:
        body["spending_policy"] = manifest["spending_policy"]
    return body | {"execution_plan_sha256": digest(body)}


def build_execution_plan(experiment_manifest: dict[str, Any]) -> dict[str, Any]:
    """Construct every immutable segment reservation under its frozen spending policy."""

    return _build_execution_plan(validate_experiment_manifest(experiment_manifest))


def validate_execution_plan(experiment_manifest: dict[str, Any], execution_plan: Any) -> dict[str, Any]:
    """Reject a rehashed schedule unless it is the exact canonical reconstruction."""

    manifest = validate_experiment_manifest(experiment_manifest)
    base_fields = {
        "schema", "experiment_manifest_sha256", "new_spend_cap_micro_usd", "reservation_total_micro_usd", "cells",
        "execution_admission", "execution_plan_sha256",
    }
    expected_schema = (
        EXECUTION_PLAN_SEQUENTIAL_SCHEMA
        if manifest["schema"] == EXPERIMENT_MANIFEST_SEQUENTIAL_SCHEMA
        else EXECUTION_PLAN_SCHEMA
    )
    fields = base_fields | ({"spending_policy"} if expected_schema == EXECUTION_PLAN_SEQUENTIAL_SCHEMA else set())
    plan = _require_exact_dict(execution_plan, fields, "execution plan")
    if plan["schema"] != expected_schema:
        raise ExamError("unsupported execution plan schema")
    expected = _build_execution_plan(manifest)
    if plan != expected:
        raise ExamError("execution plan does not match the exact canonical frozen schedule")
    return copy.deepcopy(expected)


__all__ = [
    "DEFAULT_NEW_SPEND_CAP_MICRO_USD",
    "EXECUTION_PLAN_SCHEMA",
    "EXECUTION_PLAN_SEQUENTIAL_SCHEMA",
    "EXPERIMENT_MANIFEST_SCHEMA",
    "EXPERIMENT_MANIFEST_SEQUENTIAL_SCHEMA",
    "MANIFEST_SCHEMA",
    "SPENDING_POLICY_SCHEMA",
    "USAGE_BOUND_SPENDING_POLICY_SCHEMA",
    "build_execution_plan",
    "build_experiment_manifest",
    "validate_execution_plan",
    "validate_experiment_manifest",
    "verify_benchmark",
]
