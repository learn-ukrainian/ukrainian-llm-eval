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


__all__ = ["MANIFEST_SCHEMA", "verify_benchmark"]
