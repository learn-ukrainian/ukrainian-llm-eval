from __future__ import annotations

import copy

import pytest

from ukrainian_llm_eval.core import ExamError, digest, prepare_exam
from ukrainian_llm_eval.typography import TYPOGRAPHY_RECEIPT_SCHEMA, TYPOGRAPHY_SCHEMA, apply_typography


def _exam() -> dict:
    return {
        "schema": "zno-nmt.exam.v1",
        "title": "Synthetic typography paper",
        "subject": "Ukrainian",
        "year": 2022,
        "provenance": {
            "source_url": "https://example.test/paper",
            "source_revision": "synthetic-typography-r1",
            "license": "test-only",
            "exposure": "synthetic",
        },
        "scoring": {
            "kind": "benchmark",
            "policy_url": None,
            "pass_threshold": None,
            "expected_items": 2,
            "expected_points": 3,
        },
        "items": [
            {
                "id": "single-0",
                "kind": "single",
                "question": "Оберіть *правильний* варіант.",
                "options": [{"id": "A", "text": "перший"}, {"id": "B", "text": "другий"}],
                "rows": [],
                "correct": "A",
            },
            {
                "id": "matching-1",
                "kind": "matching",
                "question": "Зіставте пари.",
                "options": [
                    {"id": "A", "text": "один"},
                    {"id": "B", "text": "два"},
                    {"id": "C", "text": "три"},
                ],
                "rows": [{"id": "r1", "text": "перше"}, {"id": "r2", "text": "друге"}],
                "correct": {"r1": "A", "r2": "B"},
            },
        ],
    }


def _overlay(exam: dict, changes: list[dict] | None = None) -> dict:
    return {
        "schema": TYPOGRAPHY_SCHEMA,
        "source_exam_sha256": digest(exam),
        "official_source": {"url": "https://example.test/official.pdf", "sha256": "f" * 64},
        "changes": [] if changes is None else changes,
    }


def test_apply_typography_binds_hashes_preserves_key_data_and_source() -> None:
    exam = _exam()
    original = copy.deepcopy(exam)
    overlay = _overlay(
        exam,
        [
            {
                "item_id": "single-0",
                "field": "question",
                "original_text": "*правильний*",
                "replacement_text": "правильний",
            },
            {
                "item_id": "matching-1",
                "field": "matching_headings",
                "left": "Назви ліворуч",
                "right": "Назви праворуч",
            },
        ],
    )

    result, receipt = apply_typography(exam, overlay)

    assert exam == original
    assert result is not exam
    assert result["items"][0]["question"] == "Оберіть правильний варіант."
    assert result["items"][1]["question"] == "Зіставте пари.\n\nНазви ліворуч → Назви праворуч"
    assert result["items"][0]["correct"] == exam["items"][0]["correct"]
    assert result["items"][0]["options"] == exam["items"][0]["options"]
    assert result["items"][1]["rows"] == exam["items"][1]["rows"]

    source_packet, source_key = prepare_exam(exam)
    result_packet, result_key = prepare_exam(result)
    assert source_key["answers"] == result_key["answers"]
    for field in ("schema", "title", "subject", "year", "provenance", "scoring"):
        assert source_key[field] == result_key[field]
    assert set(receipt) == {
        "schema",
        "source_exam_sha256",
        "result_exam_sha256",
        "source_packet_sha256",
        "result_packet_sha256",
        "overlay_sha256",
        "official_source_sha256",
        "change_count",
    }
    assert receipt["schema"] == TYPOGRAPHY_RECEIPT_SCHEMA
    assert receipt["source_exam_sha256"] == digest(exam)
    assert receipt["result_exam_sha256"] == digest(result)
    assert receipt["source_packet_sha256"] == source_packet["packet_sha256"]
    assert receipt["result_packet_sha256"] == result_packet["packet_sha256"]
    assert receipt["overlay_sha256"] == digest(overlay)
    assert receipt["official_source_sha256"] == overlay["official_source"]["sha256"]
    assert receipt["change_count"] == 2
    assert "правильний" not in repr(receipt)
    assert "Назви ліворуч" not in repr(receipt)
    assert "https://example.test/official.pdf" not in repr(receipt)


def test_empty_overlay_returns_deep_unchanged_exam() -> None:
    exam = _exam()

    result, receipt = apply_typography(exam, _overlay(exam))

    assert result == exam
    assert result is not exam
    assert result["items"] is not exam["items"]
    assert receipt["source_exam_sha256"] == receipt["result_exam_sha256"]
    assert receipt["source_packet_sha256"] == receipt["result_packet_sha256"]
    assert receipt["change_count"] == 0


def test_overlay_source_hash_and_sha256_format_are_verified() -> None:
    exam = _exam()
    tampered = _overlay(exam)
    tampered["source_exam_sha256"] = "0" * 64
    with pytest.raises(ExamError, match="source exam hash mismatch"):
        apply_typography(exam, tampered)

    malformed = _overlay(exam)
    malformed["official_source"]["sha256"] = "F" * 64
    with pytest.raises(ExamError, match="lowercase SHA-256"):
        apply_typography(exam, malformed)


@pytest.mark.parametrize(
    ("original_text", "replacement_text"),
    [
        ("*правильний*", "*інший*"),
        ("*правильний*", " *правильний*"),
        ("*правильний*", "*правильний!*"),
    ],
)
def test_question_change_rejects_lexical_whitespace_and_punctuation_changes(
    original_text: str, replacement_text: str
) -> None:
    exam = _exam()
    change = {
        "item_id": "single-0",
        "field": "question",
        "original_text": original_text,
        "replacement_text": replacement_text,
    }

    with pytest.raises(ExamError, match="beyond Markdown asterisks"):
        apply_typography(exam, _overlay(exam, [change]))


@pytest.mark.parametrize("needle", ["відсутній", "а"])
def test_question_change_requires_one_exact_occurrence(needle: str) -> None:
    exam = _exam()
    change = {
        "item_id": "matching-1",
        "field": "question",
        "original_text": needle,
        "replacement_text": needle + "*",
    }

    with pytest.raises(ExamError, match="exact occurrence"):
        apply_typography(exam, _overlay(exam, [change]))


def test_overlapping_question_occurrences_are_ambiguous() -> None:
    exam = _exam()
    exam["items"][0]["question"] = "ааа"
    change = {
        "item_id": "single-0",
        "field": "question",
        "original_text": "аа",
        "replacement_text": "а*а",
    }

    with pytest.raises(ExamError, match="ambiguous exact occurrence"):
        apply_typography(exam, _overlay(exam, [change]))


def test_duplicate_unknown_and_unsupported_changes_are_rejected() -> None:
    exam = _exam()
    change = {
        "item_id": "single-0",
        "field": "question",
        "original_text": "*правильний*",
        "replacement_text": "правильний",
    }
    with pytest.raises(ExamError, match="duplicate"):
        apply_typography(exam, _overlay(exam, [change, copy.deepcopy(change)]))

    unknown = {**change, "item_id": "missing-item"}
    with pytest.raises(ExamError, match="unknown item"):
        apply_typography(exam, _overlay(exam, [unknown]))

    unsupported = {"item_id": "single-0", "field": "options", "replacement": []}
    with pytest.raises(ExamError, match="unsupported"):
        apply_typography(exam, _overlay(exam, [unsupported]))

    extra = {**change, "options": []}
    with pytest.raises(ExamError, match="invalid fields"):
        apply_typography(exam, _overlay(exam, [extra]))


def test_matching_headings_are_single_use_and_restricted_to_matching_items() -> None:
    exam = _exam()
    heading = {
        "item_id": "matching-1",
        "field": "matching_headings",
        "left": "Ліва",
        "right": "Права",
    }
    another_heading = {**heading, "left": "Інша ліва"}
    with pytest.raises(ExamError, match="repeats matching headings"):
        apply_typography(exam, _overlay(exam, [heading, another_heading]))

    single_heading = {**heading, "item_id": "single-0"}
    with pytest.raises(ExamError, match="requires a matching item"):
        apply_typography(exam, _overlay(exam, [single_heading]))

    for key in ("left", "right"):
        newline = {**heading, key: "Назва\n"}
        with pytest.raises(ExamError, match="must not contain newlines"):
            apply_typography(exam, _overlay(exam, [newline]))

    empty = {**heading, "left": ""}
    with pytest.raises(ExamError, match="non-empty string"):
        apply_typography(exam, _overlay(exam, [empty]))

    whitespace = {**heading, "right": " \t"}
    with pytest.raises(ExamError, match="non-whitespace text"):
        apply_typography(exam, _overlay(exam, [whitespace]))
