from __future__ import annotations

import copy
import hashlib

import pytest

from ukrainian_llm_eval.core import ExamError, digest, prepare_exam, validate_packet
from ukrainian_llm_eval.gec import prepare_gec, validate_gec_packet
from ukrainian_llm_eval.segmentation import (
    SEGMENT_PLAN_SCHEMA,
    derive_segment_packet,
    derive_segment_plan,
    reassemble_cell,
    validate_segment_plan,
)
from ukrainian_llm_eval.ulp import import_ulp

PROTOCOL_SHA256 = "a" * 64


def _nmt_packet() -> dict:
    exam = {
        "schema": "zno-nmt.exam.v1",
        "title": "Synthetic NMT",
        "subject": "Ukrainian",
        "year": 2022,
        "provenance": {
            "source_url": "https://example.test/nmt",
            "source_revision": "synthetic-r1",
            "license": "test-only",
            "exposure": "synthetic",
        },
        "scoring": {
            "kind": "benchmark",
            "policy_url": None,
            "pass_threshold": None,
            "expected_items": 3,
            "expected_points": 4,
        },
        "items": [
            {
                "id": "source-one",
                "kind": "single",
                "question": "Choose one.",
                "options": [{"id": "A", "text": "one"}, {"id": "B", "text": "two"}],
                "rows": [],
                "correct": "A",
            },
            {
                "id": "source-match",
                "kind": "matching",
                "question": "Match both rows.",
                "options": [
                    {"id": "A", "text": "one"},
                    {"id": "B", "text": "two"},
                    {"id": "C", "text": "three"},
                ],
                "rows": [{"id": "r1", "text": "first"}, {"id": "r2", "text": "second"}],
                "correct": {"r1": "A", "r2": "B"},
            },
            {
                "id": "source-two",
                "kind": "single",
                "question": "Choose two.",
                "options": [{"id": "A", "text": "one"}, {"id": "B", "text": "two"}],
                "rows": [],
                "correct": "B",
            },
        ],
    }
    packet, _key = prepare_exam(exam)
    return packet


def _ulp_packet() -> dict:
    rows = [
        {
            "question": "Перше питання.",
            "choices": ["А", "Б", "В"],
            "answer": 0,
            "answer_letter": "А",
            "debug_id": "one",
            "category": "grammar",
        },
        {
            "question": "Друге питання.",
            "choices": ["А", "Б", "В"],
            "answer": 1,
            "answer_letter": "Б",
            "debug_id": "two",
            "category": "grammar",
        },
    ]
    metadata = {
        "title": "Synthetic ULP",
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
            "expected_items": 2,
            "expected_points": 2,
        },
    }
    exam = import_ulp(rows, metadata)
    packet, _key = prepare_exam(exam)
    return packet


def _noop(annotator_id: str) -> str:
    return f"A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||{annotator_id}"


def _gec_source() -> str:
    return (
        "S # 0001\n"
        f"{_noop('0')}\n"
        f"{_noop('1')}\n\n"
        "S Перше речення .\n"
        f"{_noop('0')}\n"
        f"{_noop('1')}\n\n"
        "S Друге речення .\n"
        f"{_noop('0')}\n"
        f"{_noop('1')}\n\n"
        "S # 0002\n"
        f"{_noop('0')}\n"
        f"{_noop('1')}\n\n"
        "S Третє речення .\n"
        f"{_noop('0')}\n"
        f"{_noop('1')}\n\n"
    )


def _gec_packet() -> tuple[dict, str]:
    provenance = {
        "source_url": "https://example.test/gec",
        "source_revision": "synthetic-r1",
        "license": "test-only",
        "exposure": "synthetic",
    }
    packet, _key = prepare_gec(_gec_source(), provenance)
    return packet, _gec_source()


def _plan(packet: dict, suite_id: str, denominator: dict, *, source: str | None = None) -> dict:
    return derive_segment_plan(
        packet,
        suite_id=suite_id,
        protocol_sha256=PROTOCOL_SHA256,
        denominator=denominator,
        gec_source=source,
    )


def _result(segment: dict, responses: dict, *, status: str = "ok") -> dict:
    return {
        "segment_id": segment["segment_id"],
        "packet_sha256": segment["packet_sha256"],
        "status": status,
        "responses": responses,
    }


def test_nmt_plan_is_deterministic_gold_free_and_matching_is_atomic() -> None:
    packet = _nmt_packet()
    denominator = {"items": 3, "single_choice": 2, "matching": 1, "points": 4}
    plan = _plan(packet, "nmt-2022-demo-ukrainian", denominator)

    assert plan == _plan(packet, "nmt-2022-demo-ukrainian", denominator)
    assert plan["schema"] == SEGMENT_PLAN_SCHEMA
    assert plan["source_packet_sha256"] == packet["packet_sha256"]
    assert plan["source_sha256"] is None
    assert [segment["segment_id"] for segment in plan["segments"]] == ["seg-0001", "seg-0002", "seg-0003"]
    assert [segment["item_ids"] for segment in plan["segments"]] == [["q0001"], ["q0002"], ["q0003"]]
    matching = derive_segment_packet(packet, ["q0002"])
    assert matching["items"][0]["kind"] == "matching"
    assert len(matching["items"][0]["rows"]) == 2
    validate_packet(matching)
    assert "correct" not in repr(plan)
    assert "correct" not in repr(matching)


def test_ulp_plan_makes_one_question_per_segment() -> None:
    packet = _ulp_packet()
    plan = _plan(packet, "ulp", {"items": 2, "points": 2})

    assert plan["unit"] == "ulp-question"
    assert [segment["item_ids"] for segment in plan["segments"]] == [["q0001"], ["q0002"]]
    validate_segment_plan(plan, packet, suite_id="ulp", protocol_sha256=PROTOCOL_SHA256)


def test_gec_membership_uses_source_documents_and_excludes_annotations() -> None:
    packet, source = _gec_packet()
    denominator = {"sentences": 3, "documents": 2, "tokens": 9}
    plan = _plan(packet, "ua-gec-public-gec-only-test", denominator, source=source)

    assert plan["unit"] == "ua-gec-document"
    assert plan["source_sha256"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert [segment["item_ids"] for segment in plan["segments"]] == [["q0001", "q0002"], ["q0003"]]
    assert "annotations" not in repr(plan)
    assert "replacement" not in repr(plan)
    first = derive_segment_packet(packet, plan["segments"][0]["item_ids"])
    validate_gec_packet(first)
    assert [item["id"] for item in first["items"]] == ["q0001", "q0002"]
    assert "annotations" not in repr(first)
    validate_segment_plan(plan, packet, gec_source=source)
    with pytest.raises(ExamError, match="source packet"):
        _plan(
            packet,
            "ua-gec-public-gec-only-test",
            denominator,
            source=source.replace("Третє", "Інше"),
        )


@pytest.mark.parametrize("mutation", ["omitted", "duplicate", "reordered", "foreign", "mutated"])
def test_exact_partition_validation_rejects_plan_mutations(mutation: str) -> None:
    packet = _nmt_packet()
    plan = _plan(packet, "nmt-2022-demo-ukrainian", {"items": 3, "single_choice": 2, "matching": 1, "points": 4})
    changed = copy.deepcopy(plan)
    if mutation == "omitted":
        changed["segments"] = changed["segments"][:-1]
    elif mutation == "duplicate":
        changed["segments"][1]["item_ids"] = ["q0001"]
    elif mutation == "reordered":
        changed["segments"] = [changed["segments"][1], changed["segments"][0], changed["segments"][2]]
    elif mutation == "foreign":
        changed["segments"][0]["item_ids"] = ["q9999"]
    else:
        changed["segments"][0]["item_ids"] = ["q0002"]
    changed["segment_plan_sha256"] = digest({field: changed[field] for field in changed if field != "segment_plan_sha256"})
    with pytest.raises(ExamError):
        validate_segment_plan(changed, packet)


def test_segment_packet_rejects_duplicate_reordered_and_foreign_ids() -> None:
    packet = _nmt_packet()
    for item_ids in (["q0001", "q0001"], ["q0002", "q0001"], ["q9999"]):
        with pytest.raises(ExamError):
            derive_segment_packet(packet, list(item_ids))


def test_reassembly_requires_complete_ordered_successful_segments_and_maps_local_ids() -> None:
    packet = _nmt_packet()
    plan = _plan(packet, "nmt-2022-demo-ukrainian", {"items": 3, "single_choice": 2, "matching": 1, "points": 4})
    results = [_result(segment, {"q0001": answer}) for segment, answer in zip(plan["segments"], ["A", None, "B"], strict=True)]

    assert reassemble_cell(plan, results) == {"q0001": "A", "q0002": None, "q0003": "B"}
    assert reassemble_cell(plan, {record["segment_id"]: record for record in results}) == {
        "q0001": "A",
        "q0002": None,
        "q0003": "B",
    }
    for bad_results in (
        results[:-1],
        [results[1], results[0], results[2]],
        results[:1] + [results[0], results[2]],
    ):
        with pytest.raises(ExamError):
            reassemble_cell(plan, bad_results)
    failed = copy.deepcopy(results)
    failed[1]["status"] = "failed"
    with pytest.raises(ExamError):
        reassemble_cell(plan, failed)
    mutated = copy.deepcopy(results)
    mutated[0]["packet_sha256"] = "b" * 64
    with pytest.raises(ExamError):
        reassemble_cell(plan, mutated)
    foreign_response = copy.deepcopy(results)
    foreign_response[0]["responses"] = {"q0002": "A"}
    with pytest.raises(ExamError):
        reassemble_cell(plan, foreign_response)


def test_gec_reassembly_rejects_null_or_multiline_corrections() -> None:
    packet, source = _gec_packet()
    plan = _plan(packet, "ua-gec-public-gec-only-test", {"sentences": 3, "documents": 2, "tokens": 9}, source=source)
    good = [_result(segment, {f"q{index:04d}": "Готове речення ." for index in range(1, len(segment["item_ids"]) + 1)}) for segment in plan["segments"]]
    assert list(reassemble_cell(plan, good)) == ["q0001", "q0002", "q0003"]
    null_response = copy.deepcopy(good)
    null_response[0]["responses"]["q0001"] = None
    with pytest.raises(ExamError, match="GEC"):
        reassemble_cell(plan, null_response)
    multiline = copy.deepcopy(good)
    multiline[0]["responses"]["q0001"] = "Рядок 1\nРядок 2"
    with pytest.raises(ExamError, match="GEC"):
        reassemble_cell(plan, multiline)
