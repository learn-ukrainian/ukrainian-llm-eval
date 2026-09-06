"""Real subprocess admission linked to synthetic candidate execution."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_admission import inputs as claim_inputs
from test_admission_command import _command_spec, _fixture
from test_research_scheduling import inputs, trial

from ukrainian_llm_eval import execution, scheduling
from ukrainian_llm_eval.admission import CommandAdmissionController
from ukrainian_llm_eval.admission_command import command_identity_sha256
from ukrainian_llm_eval.benchmark_manifest import build_execution_plan, build_experiment_manifest
from ukrainian_llm_eval.core import ExamError, digest
from ukrainian_llm_eval.evidence import EvidenceStore


def controller_inputs(tmp_path):
    packets, segments, manifest, _, configs = inputs()
    route = manifest["routes"][0]
    claims, _, _, _, kwargs = claim_inputs("verified_subscription")
    for kind in ("pricing", "entitlement", "capability"):
        state = claims[kind]["state"]
        state["route_sha256"] = route["route_sha256"]
        if kind == "entitlement":
            state["valid_until"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        if kind == "capability":
            state["tool_policy_sha256"] = manifest["tool_policy_sha256"]
        claims[kind]["state_sha256"] = route[kind + "_evidence_sha256"] = digest(state)
    auth = {**kwargs["operator_authorization"], "route_sha256": route["route_sha256"]}
    route["operator_authorization_sha256"] = digest(auth)
    source = "import json, sys\nfrom datetime import datetime, timezone\nr=json.load(sys.stdin)\n"
    source += "result=json.loads(" + repr(json.dumps(claims)) + ")\n"
    source += "result.update(nonce=r['nonce'], request_sha256=r['request_sha256'], observed_at=datetime.now(timezone.utc).isoformat())\nprint(json.dumps(result))\n"
    script, lock = _fixture(tmp_path, source)
    spec = _command_spec(script, lock, timeout_seconds=5.5, stdout_max_bytes=8192)
    route["admission_command_sha256"] = command_identity_sha256(spec)
    manifest = build_experiment_manifest(manifest["protocol_sha256"], manifest["suites"], [route],
                                         scorer_sha256=manifest["scorer_sha256"],
                                         tool_policy_sha256=manifest["tool_policy_sha256"])
    controller = CommandAdmissionController({"fixture": spec}, {"fixture": auth})
    return (packets, segments, manifest, build_execution_plan(manifest), configs), controller


def test_each_candidate_has_fresh_real_process_admission_and_resume_makes_no_calls(monkeypatch, tmp_path):
    args, controller = controller_inputs(tmp_path)
    assert controller.authorizations["fixture"]["allow_paid"] is False
    assert controller.authorizations["fixture"]["max_new_spend_micro_usd"] == 0
    calls = []
    monkeypatch.setattr(execution, "run_exam", trial(calls))
    root = tmp_path / "execution"
    progress = list(scheduling.run_research(*args, root, admission_probe=controller))
    assert len(progress) == 6 and all(item["status"] == "ok" for item in progress)
    approvals = EvidenceStore(root / "admission-evidence").verify_all()
    assert len(approvals) == len(calls) == 12
    assert len({item["metadata"]["request_sha256"] for item in approvals.values()}) == 12
    for candidate in EvidenceStore(root / "evidence").verify_all().values():
        context = candidate["metadata"]["segment_context"]
        assert context["admission_receipt_sha256"] == digest(approvals[context["admission_attempt_id"]])
    assert list(scheduling.run_research(*args, root, admission_probe=controller, resume=True)) == progress
    assert len(calls) == 12
    assert EvidenceStore(root / "admission-evidence").verify_all() == approvals


def test_changed_operator_authorization_prevents_all_execution(monkeypatch, tmp_path):
    args, controller = controller_inputs(tmp_path)
    controller.authorizations["fixture"]["allow_paid"] = True
    calls = []
    monkeypatch.setattr(execution, "run_exam", trial(calls))
    root = tmp_path / "execution"
    with pytest.raises(ExamError, match="authorization drift"):
        list(scheduling.run_research(*args, root, admission_probe=controller))
    assert calls == [] and not root.exists()


def test_whole_schedule_rejects_new_spend_when_allow_paid_is_false(tmp_path):
    _packets, _segments, manifest, _plan, _configs = inputs(metered=True)
    route = manifest["routes"][0]
    script, lock = _fixture(tmp_path, "print('{}')\n")
    spec = _command_spec(script, lock)
    route["admission_command_sha256"] = command_identity_sha256(spec)
    authorization = {
        "schema": "ukrainian-llm-eval.operator-authorization.v1",
        "route_sha256": route["route_sha256"],
        "allow_paid": False,
        "max_new_spend_micro_usd": 0,
    }
    route["operator_authorization_sha256"] = digest(authorization)
    manifest = build_experiment_manifest(
        manifest["protocol_sha256"], manifest["suites"], [route],
        scorer_sha256=manifest["scorer_sha256"],
        tool_policy_sha256=manifest["tool_policy_sha256"],
    )
    controller = CommandAdmissionController({"fixture": spec}, {"fixture": authorization})

    with pytest.raises(ExamError, match="unauthorized new spend"):
        controller.prepare(manifest, build_execution_plan(manifest))


def test_legacy_authorization_retains_total_reservation_meaning_for_sequential_plan(tmp_path):
    _packets, _segments, manifest, _plan, _configs = inputs(metered=True)
    route = manifest["routes"][0]
    script, lock = _fixture(tmp_path, "print('{}')\n")
    spec = _command_spec(script, lock)
    route["admission_command_sha256"] = command_identity_sha256(spec)
    authorization = {
        "schema": "ukrainian-llm-eval.operator-authorization.v1",
        "route_sha256": route["route_sha256"],
        "allow_paid": True,
        "max_new_spend_micro_usd": 11,
    }
    route["operator_authorization_sha256"] = digest(authorization)
    policy = {
        "schema": "ukrainian-llm-eval.spending-policy.v1",
        "mode": "sequential_shared_cap",
        "ledger_id": "issue5-public-evaluator",
        "authorized_cap_micro_usd": 11,
        "reservation_scope": "whole_segment_before_first_request",
        "settlement": "authoritative_final_account_charge_only",
        "cap_stop": "not_executed_budget",
    }
    manifest = build_experiment_manifest(
        manifest["protocol_sha256"], manifest["suites"], [route],
        scorer_sha256=manifest["scorer_sha256"], tool_policy_sha256=manifest["tool_policy_sha256"],
        new_spend_cap_micro_usd=11, spending_policy=policy,
    )
    plan = build_execution_plan(manifest)
    assert plan["reservation_total_micro_usd"] > authorization["max_new_spend_micro_usd"]
    with pytest.raises(ExamError, match="exceeds operator authorization"):
        CommandAdmissionController({"fixture": spec}, {"fixture": authorization}).prepare(manifest, plan)


def test_sequential_authorization_binds_shared_policy_and_segment_cap(tmp_path):
    _packets, _segments, manifest, _plan, _configs = inputs(metered=True)
    route = manifest["routes"][0]
    script, lock = _fixture(tmp_path, "print('{}')\n")
    spec = _command_spec(script, lock)
    route["admission_command_sha256"] = command_identity_sha256(spec)
    policy = {
        "schema": "ukrainian-llm-eval.spending-policy.v1",
        "mode": "sequential_shared_cap",
        "ledger_id": "issue5-public-evaluator",
        "authorized_cap_micro_usd": 11,
        "reservation_scope": "whole_segment_before_first_request",
        "settlement": "authoritative_final_account_charge_only",
        "cap_stop": "not_executed_budget",
    }
    manifest = build_experiment_manifest(
        manifest["protocol_sha256"], manifest["suites"], [route],
        scorer_sha256=manifest["scorer_sha256"], tool_policy_sha256=manifest["tool_policy_sha256"],
        new_spend_cap_micro_usd=11, spending_policy=policy,
    )
    plan = build_execution_plan(manifest)
    authorization = {
        "schema": "ukrainian-llm-eval.operator-authorization.v2",
        "route_sha256": route["route_sha256"],
        "allow_paid": True,
        "max_segment_new_spend_micro_usd": 11,
        "spending_policy_sha256": digest(plan["spending_policy"]),
    }
    route["operator_authorization_sha256"] = digest(authorization)
    manifest = build_experiment_manifest(
        manifest["protocol_sha256"], manifest["suites"], [route],
        scorer_sha256=manifest["scorer_sha256"], tool_policy_sha256=manifest["tool_policy_sha256"],
        new_spend_cap_micro_usd=11, spending_policy=policy,
    )
    plan = build_execution_plan(manifest)
    controller = CommandAdmissionController({"fixture": spec}, {"fixture": authorization})
    controller.prepare(manifest, plan)

    drifted_plan = {**plan, "spending_policy": {**plan["spending_policy"], "ledger_id": "other-ledger"}}
    with pytest.raises(ExamError, match="spending policy drift"):
        controller.prepare(manifest, drifted_plan)
