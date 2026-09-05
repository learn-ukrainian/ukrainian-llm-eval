"""Apply a source-bound, typography-only overlay to a validated exam.

The overlay records corrections that were reviewed against an official source,
but this module does not fetch or inspect that source.  Its declared source
digest is validated as a SHA-256 value and retained in the receipt; the receipt
therefore proves source binding and the allowed local transformation only.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .core import ExamError, digest, prepare_exam

TYPOGRAPHY_SCHEMA = "ukrainian-llm-eval.typography.v1"
TYPOGRAPHY_RECEIPT_SCHEMA = "ukrainian-llm-eval.typography.receipt.v1"

_OVERLAY_FIELDS = {"schema", "source_exam_sha256", "official_source", "changes"}
_OFFICIAL_SOURCE_FIELDS = {"url", "sha256"}
_QUESTION_CHANGE_FIELDS = {"item_id", "field", "original_text", "replacement_text"}
_HEADINGS_CHANGE_FIELDS = {"item_id", "field", "left", "right"}
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def _exact_dict(value: Any, fields: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExamError(f"{where} must be an object")
    if set(value) != fields:
        missing = sorted(fields - set(value))
        extra = sorted(set(value) - fields)
        detail: list[str] = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unknown {extra}")
        raise ExamError(f"{where} has invalid fields ({'; '.join(detail)})")
    return value


def _string(value: Any, where: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise ExamError(f"{where} must be a {qualifier}string")
    return value


def _sha256(value: Any, where: str) -> str:
    text = _string(value, where)
    if _SHA256_RE.fullmatch(text) is None:
        raise ExamError(f"{where} must be a lowercase SHA-256 hex digest")
    return text


def _contains_newline(value: str) -> bool:
    """Return whether *value* contains any Unicode line separator."""

    separators = ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
    return any(separator in value for separator in separators)


def _unique_occurrence(text: str, needle: str, where: str) -> int:
    """Return the sole occurrence offset, counting overlapping matches."""

    positions: list[int] = []
    start = 0
    while True:
        offset = text.find(needle, start)
        if offset < 0:
            break
        positions.append(offset)
        start = offset + 1
    if len(positions) != 1:
        qualifier = "missing" if not positions else "ambiguous"
        raise ExamError(f"{where} has {qualifier} exact occurrence")
    return positions[0]


def _without_asterisks(value: str) -> str:
    """Remove Markdown emphasis markers while preserving every other character."""

    return value.replace("*", "")


def _validate_overlay(overlay: Any, exam: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = _exact_dict(overlay, _OVERLAY_FIELDS, "overlay")
    if value["schema"] != TYPOGRAPHY_SCHEMA:
        raise ExamError(f"unsupported typography overlay schema: {value['schema']!r}")

    source_exam_sha256 = _sha256(value["source_exam_sha256"], "overlay.source_exam_sha256")
    expected_source_sha256 = digest(exam)
    if source_exam_sha256 != expected_source_sha256:
        raise ExamError("typography overlay source exam hash mismatch")

    official_source = _exact_dict(value["official_source"], _OFFICIAL_SOURCE_FIELDS, "overlay.official_source")
    _string(official_source["url"], "overlay.official_source.url")
    _sha256(official_source["sha256"], "overlay.official_source.sha256")

    changes = value["changes"]
    if not isinstance(changes, list):
        raise ExamError("overlay.changes must be a list")

    items = exam["items"]
    item_ids = {item["id"] for item in items}
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, raw_change in enumerate(changes):
        where = f"overlay.changes[{index}]"
        if not isinstance(raw_change, dict):
            raise ExamError(f"{where} must be an object")
        field = raw_change.get("field")
        if field == "question":
            change = _exact_dict(raw_change, _QUESTION_CHANGE_FIELDS, where)
        elif field == "matching_headings":
            change = _exact_dict(raw_change, _HEADINGS_CHANGE_FIELDS, where)
        else:
            raise ExamError(f"{where}.field is unsupported")

        identity = digest(change)
        if identity in seen:
            raise ExamError(f"duplicate typography change at {where}")
        seen.add(identity)

        item_id = _string(change["item_id"], f"{where}.item_id")
        if item_id not in item_ids:
            raise ExamError(f"{where}.item_id references an unknown item")

        if field == "question":
            original_text = _string(change["original_text"], f"{where}.original_text")
            replacement_text = _string(change["replacement_text"], f"{where}.replacement_text", nonempty=False)
            if _without_asterisks(original_text) != _without_asterisks(replacement_text):
                raise ExamError(f"{where} changes text beyond Markdown asterisks")
            validated.append(
                {
                    "item_id": item_id,
                    "field": field,
                    "original_text": original_text,
                    "replacement_text": replacement_text,
                }
            )
        else:
            item = next(item for item in items if item["id"] == item_id)
            if item["kind"] != "matching":
                raise ExamError(f"{where} requires a matching item")
            left = _string(change["left"], f"{where}.left")
            right = _string(change["right"], f"{where}.right")
            if not left.strip() or not right.strip():
                raise ExamError(f"{where}.left and right must contain non-whitespace text")
            if _contains_newline(left) or _contains_newline(right):
                raise ExamError(f"{where}.left and right must not contain newlines")
            if any(existing["item_id"] == item_id and existing["field"] == field for existing in validated):
                raise ExamError(f"{where} repeats matching headings for an item")
            validated.append({"item_id": item_id, "field": field, "left": left, "right": right})

    return official_source, validated


def apply_typography(exam: dict[str, Any], overlay: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply an approved typography overlay and return the transformed exam and receipt.

    The input and output exams are validated by :func:`prepare_exam`.  The
    returned exam is a deep copy, and only question strings can be changed.
    ``official_source.sha256`` is a caller-declared digest; no source retrieval
    or independent PDF-fidelity check is performed here.
    """

    source_exam = copy.deepcopy(exam)
    source_packet, _source_key = prepare_exam(source_exam)
    source_exam_sha256 = digest(exam)
    official_source, changes = _validate_overlay(overlay, exam)

    result_exam = copy.deepcopy(exam)
    result_items = {item["id"]: item for item in result_exam["items"]}
    for change in changes:
        item = result_items[change["item_id"]]
        if change["field"] == "question":
            question = item["question"]
            offset = _unique_occurrence(question, change["original_text"], f"change for {change['item_id']!r}")
            original_text = change["original_text"]
            replacement_text = change["replacement_text"]
            item["question"] = question[:offset] + replacement_text + question[offset + len(original_text) :]
        else:
            item["question"] += f"\n\n{change['left']} → {change['right']}"

    result_packet, _result_key = prepare_exam(copy.deepcopy(result_exam))
    receipt = {
        "schema": TYPOGRAPHY_RECEIPT_SCHEMA,
        "source_exam_sha256": source_exam_sha256,
        "result_exam_sha256": digest(result_exam),
        "source_packet_sha256": source_packet["packet_sha256"],
        "result_packet_sha256": result_packet["packet_sha256"],
        "overlay_sha256": digest(overlay),
        "official_source_sha256": official_source["sha256"],
        "change_count": len(changes),
    }
    return result_exam, receipt


__all__ = [
    "TYPOGRAPHY_RECEIPT_SCHEMA",
    "TYPOGRAPHY_SCHEMA",
    "apply_typography",
]
