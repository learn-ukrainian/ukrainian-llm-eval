from __future__ import annotations

import copy
import json

import pytest

from ukrainian_llm_eval.core import ExamError, prepare_exam, score_run, validate_key, validate_packet
from ukrainian_llm_eval.ulp import import_ulp, ulp_sidecar


def _metadata(item_count: int) -> dict:
    return {
        "title": "Synthetic ULP fixture",
        "subject": "Ukrainian",
        "year": 2024,
        "provenance": {
            "source_url": "https://example.test/ulp",
            "source_revision": "synthetic-r1",
            "license": "test-only",
            "exposure": "synthetic",
        },
        "scoring": {
            "kind": "benchmark",
            "policy_url": None,
            "pass_threshold": None,
            "expected_items": item_count,
            "expected_points": item_count,
        },
    }


def _row(
    *,
    question: str = "Оберіть правильний варіант.",
    choices: list[str] | None = None,
    answer: int = 1,
    answer_letter: str = "Б",
    debug_id: str = "101",
    category: str = "лексика",
) -> dict:
    return {
        "question": question,
        "choices": ["перший", "другий", "третій", "четвертий"] if choices is None else choices,
        "answer": answer,
        "answer_letter": answer_letter,
        "debug_id": debug_id,
        "category": category,
    }


def _run(packet: dict, responses: dict) -> dict:
    return {
        "schema": "zno-nmt.run.v1",
        "packet_sha256": packet["packet_sha256"],
        "condition": "closed-book",
        "status": "ok",
        "responses": responses,
        "identity": {"model": "synthetic-model", "harness": "test-harness", "effective_effort": "high"},
        "comparison": {"prompt_sha256": "synthetic-prompt"},
        "metrics": {},
    }


def test_import_accepts_four_and_five_choices_and_keeps_packet_opaque() -> None:
    rows = [
        _row(debug_id="101", category="лексика"),
        _row(
            choices=["один", "два", "три", "чотири", "п'ять"],
            answer=4,
            answer_letter="Д",
            debug_id="102",
            category="граматика",
        ),
    ]

    exam = import_ulp(rows, _metadata(len(rows)))
    assert exam["schema"] == "zno-nmt.exam.v1"
    assert [item["id"] for item in exam["items"]] == ["ulp-0001", "ulp-0002"]
    assert [[choice["id"] for choice in item["options"]] for item in exam["items"]] == [
        ["А", "Б", "В", "Г"],
        ["А", "Б", "В", "Г", "Д"],
    ]
    assert [item["correct"] for item in exam["items"]] == ["Б", "Д"]

    packet, key = prepare_exam(exam)
    validate_packet(packet)
    validate_key(packet, key)
    assert all(set(item) == {"id", "kind", "question", "options", "rows"} for item in packet["items"])
    assert "debug_id" not in json.dumps(packet, ensure_ascii=False)
    assert "category" not in json.dumps(packet, ensure_ascii=False)
    assert key["answers"] == {"q0001": "Б", "q0002": "Д"}


def test_import_accepts_three_choices() -> None:
    row = _row(choices=["один", "два", "три"], answer=2, answer_letter="В", debug_id="103")

    exam = import_ulp([row], _metadata(1))

    assert [choice["id"] for choice in exam["items"][0]["options"]] == ["А", "Б", "В"]
    assert exam["items"][0]["correct"] == "В"


@pytest.mark.parametrize("choice_count", [1, 2])
def test_import_rejects_fewer_than_three_choices(choice_count: int) -> None:
    row = _row(choices=[f"варіант-{index}" for index in range(choice_count)], answer=0, answer_letter="А")

    with pytest.raises(ExamError, match="3 to 5 choices"):
        import_ulp([row], _metadata(1))


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda row: row.pop("category"), "invalid fields"),
        (lambda row: row.update(question="   "), "question"),
        (lambda row: row.update(choices=[]), "choices"),
        (lambda row: row.update(choices=["ok", "   ", "third", "fourth"]), "choices"),
        (lambda row: row.update(answer=True), "answer"),
        (lambda row: row.update(answer=4, answer_letter="Д"), "out of range"),
        (lambda row: row.update(answer_letter="А"), "answer_letter"),
        (lambda row: row.update(debug_id=""), "debug_id"),
        (lambda row: row.update(category="\t"), "category"),
    ],
)
def test_invalid_ulp_rows_are_rejected(mutate, match: str) -> None:
    row = _row()
    mutate(row)
    with pytest.raises(ExamError, match=match):
        import_ulp([row], _metadata(1))


def test_duplicate_debug_ids_are_rejected() -> None:
    first = _row(debug_id="same")
    second = _row(debug_id="same", answer=0, answer_letter="А")
    with pytest.raises(ExamError, match="duplicate.*debug_id"):
        import_ulp([first, second], _metadata(2))


def test_sidecar_preserves_source_metadata_without_grading_or_candidate_content() -> None:
    rows = [
        _row(debug_id="201", category="орфографія"),
        _row(debug_id="202", category="синтаксис", answer=0, answer_letter="А"),
    ]
    sidecar = ulp_sidecar(rows)
    assert sidecar == {
        "schema": "zno-nmt.ulp-sidecar.v1",
        "items": [
            {"id": "q0001", "debug_id": "201", "category": "орфографія"},
            {"id": "q0002", "debug_id": "202", "category": "синтаксис"},
        ],
    }
    serialized = json.dumps(sidecar, ensure_ascii=False)
    for forbidden in ("Оберіть", "перший", "answer", "choices", "correct"):
        assert forbidden not in serialized


def test_core_scoring_reuses_ulp_answer_letters_and_does_not_need_sidecar() -> None:
    rows = [
        _row(debug_id="301", category="лексика"),
        _row(debug_id="302", category="граматика", answer=0, answer_letter="А"),
    ]
    packet, key = prepare_exam(import_ulp(rows, _metadata(2)))
    score = score_run(packet, key, _run(packet, {"q0001": "Б", "q0002": "В"}))
    assert score["raw_points"] == 1
    assert score["max_points"] == 2
    assert score["correct_items"] == 1
    assert score["missing_items"] == 0
    assert score["invalid_items"] == 0
    assert score["passed"] is None

    wrong = copy.deepcopy(packet)
    assert all("correct" not in item for item in wrong["items"])
