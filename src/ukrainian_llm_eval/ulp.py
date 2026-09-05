"""Import ULP multiple-choice rows into the core exam schema.

The ULP source keeps a source ``debug_id`` and a category beside each
question.  Those fields are useful to the private scorer, but they are not
part of the core exam or candidate packet schemas.  :func:`ulp_sidecar`
preserves them separately, keyed by the deterministic opaque packet id that
``prepare_exam`` assigns by row order.
"""

from __future__ import annotations

from typing import Any

from .core import ExamError, prepare_exam

_EXAM_SCHEMA = "zno-nmt.exam.v1"
_SIDECAR_SCHEMA = "zno-nmt.ulp-sidecar.v1"
_ROW_FIELDS = {"question", "choices", "answer", "answer_letter", "debug_id", "category"}
_METADATA_FIELDS = {"title", "subject", "year", "provenance", "scoring"}
_ANSWER_LETTERS = "АБВГД"


def _required_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExamError(f"{where} must be a non-empty string")
    return value


def _normalize_rows(rows: list) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not isinstance(rows, list):
        raise ExamError("ULP input must be an array of rows")
    if not rows:
        raise ExamError("ULP input must contain at least one row")

    items: list[dict[str, Any]] = []
    sidecar_items: list[dict[str, str]] = []
    seen_debug_ids: set[str] = set()
    for index, raw_row in enumerate(rows):
        where = f"ULP row {index}"
        if not isinstance(raw_row, dict):
            raise ExamError(f"{where} must be an object")
        if set(raw_row) != _ROW_FIELDS:
            missing = sorted(_ROW_FIELDS - set(raw_row))
            extra = sorted(set(raw_row) - _ROW_FIELDS)
            detail: list[str] = []
            if missing:
                detail.append(f"missing {missing}")
            if extra:
                detail.append(f"unknown {extra}")
            raise ExamError(f"{where} has invalid fields ({'; '.join(detail)})")

        question = _required_text(raw_row["question"], f"{where}.question")
        choices = raw_row["choices"]
        if not isinstance(choices, list) or not 3 <= len(choices) <= len(_ANSWER_LETTERS):
            raise ExamError(f"{where}.choices must contain 3 to {len(_ANSWER_LETTERS)} choices")
        normalized_choices = [
            {"id": _ANSWER_LETTERS[choice_index], "text": _required_text(choice, f"{where}.choices[{choice_index}]")}
            for choice_index, choice in enumerate(choices)
        ]

        answer = raw_row["answer"]
        if type(answer) is not int:
            raise ExamError(f"{where}.answer must be an integer")
        if answer < 0 or answer >= len(choices):
            raise ExamError(f"{where}.answer is out of range for {len(choices)} choices")

        answer_letter = _required_text(raw_row["answer_letter"], f"{where}.answer_letter")
        expected_letter = _ANSWER_LETTERS[answer]
        if answer_letter != expected_letter:
            raise ExamError(
                f"{where}.answer_letter does not match answer index "
                f"{answer} (expected {expected_letter!r})"
            )

        debug_id = _required_text(raw_row["debug_id"], f"{where}.debug_id")
        if debug_id in seen_debug_ids:
            raise ExamError(f"duplicate ULP debug_id {debug_id!r}")
        seen_debug_ids.add(debug_id)
        category = _required_text(raw_row["category"], f"{where}.category")

        item_id = f"ulp-{index + 1:04d}"
        items.append(
            {
                "id": item_id,
                "kind": "single",
                "question": question,
                "options": normalized_choices,
                "rows": [],
                "correct": answer_letter,
            }
        )
        sidecar_items.append(
            {
                "id": f"q{index + 1:04d}",
                "debug_id": debug_id,
                "category": category,
            }
        )
    return items, sidecar_items


def _validate_metadata(metadata: dict) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise ExamError("metadata must be an object")
    if set(metadata) != _METADATA_FIELDS:
        missing = sorted(_METADATA_FIELDS - set(metadata))
        extra = sorted(set(metadata) - _METADATA_FIELDS)
        detail: list[str] = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unknown {extra}")
        raise ExamError(f"metadata has invalid fields ({'; '.join(detail)})")
    return {field: metadata[field] for field in _METADATA_FIELDS}


def import_ulp(rows: list, metadata: dict) -> dict:
    """Import validated ULP rows as a ``zno-nmt.exam.v1`` exam.

    The generic importer accepts any non-empty row count.  The release
    profile is responsible for requiring the pinned source's 347 rows.
    ``prepare_exam`` is called before returning so the normalized exam has the
    same validation and scoring contract as every other core exam.
    """

    items, _sidecar_items = _normalize_rows(rows)
    normalized_metadata = _validate_metadata(metadata)
    exam = {"schema": _EXAM_SCHEMA, **normalized_metadata, "items": items}
    prepare_exam(exam)
    return exam


def ulp_sidecar(rows: list) -> dict[str, Any]:
    """Return private ULP category/debug-id metadata keyed by packet id.

    The sidecar intentionally contains no question text, choices, answer
    indices, answer letters, or grading values.  Callers should keep it with
    private preparation/scoring artifacts and never provide it to a candidate.
    """

    _items, sidecar_items = _normalize_rows(rows)
    return {"schema": _SIDECAR_SCHEMA, "items": sidecar_items}
