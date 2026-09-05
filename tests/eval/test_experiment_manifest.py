import copy

import pytest

from ukrainian_llm_eval.benchmark_manifest import (
    build_execution_plan,
    build_experiment_manifest,
    validate_execution_plan,
    validate_experiment_manifest,
)
from ukrainian_llm_eval.core import ExamError, digest


def _experiment_segment_plan(suite_id: str) -> dict:
    denominator, unit = {
        "ulp": ({"items": 2, "points": 2}, "ulp-question"),
        "nmt-2022-demo-ukrainian": (
            {"items": 2, "single_choice": 2, "matching": 0, "points": 2}, "nmt-task"
        ),
    }[suite_id]
    body = {
        "schema": "ukrainian-llm-eval.segment-plan.v1",
        "protocol_sha256": "1" * 64,
        "suite_id": suite_id,
        "source_packet_sha256": "2" * 64,
        "source_sha256": None,
        "denominator": denominator,
        "unit": unit,
        "segments": [
            {"segment_id": "seg-0001", "item_ids": ["item-a"], "packet_sha256": "3" * 64},
            {"segment_id": "seg-0002", "item_ids": ["item-b"], "packet_sha256": "4" * 64},
        ],
    }
    return body | {"segment_plan_sha256": digest(body)}


def _experiment_suite(suite_id: str = "ulp") -> dict:
    return {
        "suite_id": suite_id,
        "source_sha256": "5" * 64,
        "profile_sha256": "6" * 64,
        "segment_plan": _experiment_segment_plan(suite_id),
        "limits": {"timeout_seconds": 30, "max_output_tokens": 200, "max_tool_calls": 1},
    }


def _metered_route(route_id: str = "route-a", *, conditions: list[str] | None = None) -> dict:
    conditions = ["closed-book", "sources"] if conditions is None else conditions
    unavailable = {"closed-book", "sources"} - set(conditions)
    return {
        "route_id": route_id,
        "route_sha256": "7" * 64,
        "config_sha256": "8" * 64,
        "conditions": conditions,
        "unsupported_condition_evidence": {condition: "9" * 64 for condition in unavailable},
        "capability_evidence_sha256": "a" * 64,
        "pricing_evidence_sha256": "b" * 64,
        "entitlement_evidence_sha256": None,
        "billing": {
            "kind": "metered",
            "units": "tokens",
            "input_micro_usd_per_million_tokens": 10_000,
            "output_micro_usd_per_million_tokens": 20_000,
            "max_total_input_tokens": 100,
            "max_total_output_tokens": 400,
            "max_tool_rounds": 1,
            "tool_round_micro_usd": 9,
        },
    }


def _experiment_manifest(*, cap: int = 1_000, routes: list[dict] | None = None, suites: list[dict] | None = None) -> dict:
    return build_experiment_manifest(
        "1" * 64,
        [_experiment_suite()] if suites is None else suites,
        [_metered_route()] if routes is None else routes,
        scorer_sha256="c" * 64,
        tool_policy_sha256="d" * 64,
        new_spend_cap_micro_usd=cap,
    )


def _rehash_plan(plan: dict) -> None:
    plan["execution_plan_sha256"] = digest(
        {key: value for key, value in plan.items() if key != "execution_plan_sha256"}
    )


def test_experiment_manifest_builds_exact_counterbalanced_multi_suite_route_schedule() -> None:
    manifest = _experiment_manifest(
        routes=[_metered_route("route-a"), _metered_route("route-b", conditions=["closed-book"])],
        suites=[_experiment_suite("ulp"), _experiment_suite("nmt-2022-demo-ukrainian")],
    )
    plan = build_execution_plan(manifest)

    assert manifest["evidence_status"] == "bound_not_verified"
    assert manifest["execution_admission"] == "requires_authoritative_runtime_preflight"
    assert len(plan["cells"]) == 18
    assert plan["reservation_total_micro_usd"] == 648
    assert all(len(cell["cell_id"]) <= 64 for cell in plan["cells"])
    assert all(len(segment["reservation_id"]) <= 64 for cell in plan["cells"] for segment in cell["segments"])

    paired = [cell for cell in plan["cells"] if cell["suite_id"] == "ulp" and cell["route_id"] == "route-a"]
    assert [cell["condition"] for cell in paired] == [
        "closed-book", "sources", "sources", "closed-book", "closed-book", "sources",
    ]
    assert {cell["derived_config_sha256"] for cell in paired} == {paired[0]["derived_config_sha256"]}
    assert len({cell["derived_config_sha256"] for cell in plan["cells"] if cell["suite_id"] == "ulp"}) == 1
    assert all(
        [segment["segment_id"] for segment in cell["segments"]] == ["seg-0001", "seg-0002"] for cell in paired
    )
    assert validate_experiment_manifest(manifest) == manifest
    assert validate_execution_plan(manifest, plan) == plan


@pytest.mark.parametrize("mutation", ["drop", "duplicate", "reorder"])
def test_execution_plan_rejects_rehashed_segment_omission_duplicate_or_reorder(mutation: str) -> None:
    manifest = _experiment_manifest()
    plan = build_execution_plan(manifest)
    segments = plan["cells"][0]["segments"]
    if mutation == "drop":
        segments.pop()
    elif mutation == "duplicate":
        segments.append(copy.deepcopy(segments[0]))
    else:
        segments.reverse()
    _rehash_plan(plan)

    with pytest.raises(ExamError, match="exact canonical frozen schedule"):
        validate_execution_plan(manifest, plan)


def test_manifest_and_execution_plan_reject_hash_and_protocol_drift() -> None:
    manifest = _experiment_manifest()
    plan = build_execution_plan(manifest)
    drifted = copy.deepcopy(manifest)
    drifted["scorer_sha256"] = "e" * 64

    with pytest.raises(ExamError, match="canonical frozen construction"):
        validate_experiment_manifest(drifted)

    drifted["experiment_manifest_sha256"] = digest(
        {key: value for key, value in drifted.items() if key != "experiment_manifest_sha256"}
    )
    with pytest.raises(ExamError, match="exact canonical frozen schedule"):
        validate_execution_plan(drifted, plan)

    protocol_drift = copy.deepcopy(manifest)
    segment_plan = protocol_drift["suites"][0]["segment_plan"]
    segment_plan["protocol_sha256"] = "f" * 64
    segment_plan["segment_plan_sha256"] = digest(
        {key: value for key, value in segment_plan.items() if key != "segment_plan_sha256"}
    )
    protocol_drift["experiment_manifest_sha256"] = digest(
        {key: value for key, value in protocol_drift.items() if key != "experiment_manifest_sha256"}
    )
    with pytest.raises(ExamError, match="does not match experiment protocol"):
        validate_experiment_manifest(protocol_drift)


def test_metered_unknown_pricing_and_cap_overrun_block_construction() -> None:
    route = _metered_route()
    route["billing"]["input_micro_usd_per_million_tokens"] = 0
    route["billing"]["output_micro_usd_per_million_tokens"] = 0
    route["billing"]["tool_round_micro_usd"] = 0
    with pytest.raises(ExamError, match="unknown or zero-only metered pricing"):
        _experiment_manifest(routes=[route])

    with pytest.raises(ExamError, match="new-spend cap"):
        build_execution_plan(_experiment_manifest(cap=215))


def test_route_limits_and_entitlements_are_conservative_and_receipt_bound() -> None:
    route = _metered_route(conditions=["sources"])
    route["billing"]["max_total_output_tokens"] = 399
    with pytest.raises(ExamError, match="output bound"):
        _experiment_manifest(routes=[route])

    entitled = _metered_route()
    entitled["billing"] = entitled["billing"] | {
        "kind": "verified_subscription",
        "input_micro_usd_per_million_tokens": 0,
        "output_micro_usd_per_million_tokens": 0,
        "tool_round_micro_usd": 0,
    }
    with pytest.raises(ExamError, match="entitlement_evidence_sha256"):
        _experiment_manifest(routes=[entitled])

    entitled["entitlement_evidence_sha256"] = "f" * 64
    manifest = _experiment_manifest(routes=[entitled])
    plan = build_execution_plan(manifest)
    assert plan["reservation_total_micro_usd"] == 0
    assert manifest["execution_admission"] == "requires_authoritative_runtime_preflight"
