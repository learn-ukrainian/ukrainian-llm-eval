"""Offline mini prepare → score → compare path for Sources readiness (#45).

Live Sources gold for the second item is the integer match_count from
sources.tool-result.v1 for verify_word(\"синій\"), documented as 6 in the LU
exam/eval pack. Option texts carry that count; the answer remains option id A.
match_count is never itself an MCQ letter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ukrainian_llm_eval import adapters, compare_runs, prepare_exam, score_run

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mini_sources_exam.json"

# Contract note: LU Sources exam pack (epic #7953 / #7955) uses match_count=6
# for verify_word("синій"). Reconfirm against a live MCP before scored admission.
SOURCES_MATCH_COUNT_GOLD = 6
SOURCES_MATCH_COUNT_OPTION_ID = "A"


def _exam() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _run(
    packet: dict,
    *,
    condition: str,
    responses: dict,
    metrics: dict | None = None,
) -> dict:
    return {
        "schema": "zno-nmt.run.v1",
        "packet_sha256": packet["packet_sha256"],
        "condition": condition,
        "status": "ok",
        "responses": responses,
        "identity": {
            "model": "synthetic-mini",
            "harness": "test-harness",
            "effective_effort": "low",
        },
        "comparison": {"prompt_sha256": "mini-prompt"},
        "metrics": {} if metrics is None else metrics,
    }


def test_mini_exam_gold_maps_match_count_to_option_id_not_raw_count() -> None:
    exam = _exam()
    assert exam["items"][1]["correct"] == SOURCES_MATCH_COUNT_OPTION_ID
    gold_text = next(
        option["text"]
        for option in exam["items"][1]["options"]
        if option["id"] == SOURCES_MATCH_COUNT_OPTION_ID
    )
    assert gold_text == str(SOURCES_MATCH_COUNT_GOLD)
    assert SOURCES_MATCH_COUNT_OPTION_ID != str(SOURCES_MATCH_COUNT_GOLD)


def test_mini_prepare_score_compare_is_fully_offline() -> None:
    packet, key = prepare_exam(_exam())
    assert [item["id"] for item in packet["items"]] == ["q0001", "q0002"]
    assert key["answers"] == {"q0001": "A", "q0002": "A"}

    closed = _run(
        packet,
        condition="closed-book",
        responses={"q0001": "A", "q0002": "B"},
        metrics={"tool_calls": 0},
    )
    sources = _run(
        packet,
        condition="sources",
        responses={"q0001": "A", "q0002": "A"},
        metrics={"tool_calls": 1},
    )

    closed_score = score_run(packet, key, closed)
    sources_score = score_run(packet, key, sources)
    assert closed_score["raw_points"] == 1
    assert sources_score["raw_points"] == 2
    assert sources_score["items"][1]["outcome"] == "correct"

    comparison = compare_runs(packet, key, closed, sources)
    assert comparison["control_points"] == 1
    assert comparison["treatment_points"] == 2
    assert comparison["score_delta"] == 1


def test_mini_invalid_raw_match_count_fails_extract_not_ok_run() -> None:
    packet, _key = prepare_exam(_exam())
    with pytest.raises(adapters.AdapterError, match="provider response value is invalid"):
        adapters._extract_responses({"responses": {"q0001": "A", "q0002": "6"}}, packet)


def test_mini_sources_prompt_requires_structured_decision_use() -> None:
    packet, _key = prepare_exam(_exam())
    prompt = adapters.build_prompt(packet, "sources", max_tool_calls=4)
    assert "structured results" in prompt
    assert "match_count" in prompt
    assert "not itself an option id" in prompt
    assert "exactly one listed option id" in prompt
