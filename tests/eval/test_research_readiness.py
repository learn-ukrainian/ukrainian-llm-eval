"""Actual CLI admissions with synthetic claims and no candidate execution."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_admission import inputs as claim_inputs
from test_admission_command import _command_spec, _fixture
from test_research_scheduling import inputs

from ukrainian_llm_eval import adapters, execution, readiness, scheduling
from ukrainian_llm_eval.admission import CommandAdmissionController
from ukrainian_llm_eval.admission_command import command_identity_sha256
from ukrainian_llm_eval.benchmark_manifest import build_execution_plan, build_experiment_manifest
from ukrainian_llm_eval.core import ExamError, digest
from ukrainian_llm_eval.evidence import EvidenceStore
from ukrainian_llm_eval.segmentation import derive_segment_plan

SOURCES = "https://sources.example.invalid/mcp"


def fixture(tmp_path, *, fail_large=False, paid_state=None):
    packets, segments, manifest, _, configs = inputs()
    packet = packets["ulp"]
    packet["items"][1]["question"] += " довге питання" * 50
    packet["packet_sha256"] = digest({key: value for key, value in packet.items() if key != "packet_sha256"})
    segments["ulp"] = derive_segment_plan(packet, suite_id="ulp", protocol_sha256=manifest["protocol_sha256"],
                                         denominator={"items": 2, "points": 2})
    manifest["suites"][0]["segment_plan"] = segments["ulp"]
    # Suite configuration must win over the valid but smaller base limit.
    configs["fixture"]["max_output_tokens"] = 50
    configs["fixture"]["tools"] = ["search_text"]
    route = manifest["routes"][0]
    policy = None
    if paid_state is not None:
        from test_request_budget import _provider_bound_values

        prior = tmp_path / "prior"
        prior.mkdir()
        paid_route, _, budget_controller, _ = _provider_bound_values(prior)
        route["billing"].update(paid_route["billing"])
        configs["fixture"].update(adapter="chat-http", provider="fixture-provider", endpoint_env="FIXTURE_ENDPOINT")
        policy = budget_controller._spending_policy
        paid_state.update(controller=budget_controller, ledger=budget_controller._shared_ledger,
                          ledger_path=budget_controller._shared_ledger_path)
    route["config_sha256"] = digest(configs["fixture"])
    route["route_sha256"] = execution.route_fingerprint(configs["fixture"], SOURCES)
    claims, _, _, _, kwargs = claim_inputs("metered" if paid_state is not None else "verified_subscription")
    if paid_state is not None:
        for field in ("input_micro_usd_per_million_tokens", "output_micro_usd_per_million_tokens", "tool_round_micro_usd"):
            claims["pricing"]["state"][field] = route["billing"][field]
        claims["pricing"]["observed"] = {"conservative_segment_cost_micro_usd": 606,
                                           "incremental_segment_cost_micro_usd": 606}
        claims["capability"]["observed"]["required_input_tokens"] = 50
    for kind in ("pricing", "entitlement", "capability"):
        state = claims[kind]["state"]
        state["route_sha256"] = route["route_sha256"]
        if kind == "entitlement":
            state["valid_until"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        if kind == "capability":
            state["tool_policy_sha256"] = manifest["tool_policy_sha256"]
        claims[kind]["state_sha256"] = route[kind + "_evidence_sha256"] = digest(state)
    auth = {**kwargs["operator_authorization"], "route_sha256": route["route_sha256"]}
    if paid_state is not None:
        auth = {"schema": "ukrainian-llm-eval.operator-authorization.v2",
                "route_sha256": route["route_sha256"], "allow_paid": True,
                "max_segment_new_spend_micro_usd": 606, "spending_policy_sha256": digest(policy)}
        mechanism = paid_state["controller"].route_specs["fixture"]["mechanism"]
        mechanism.update(route_sha256=route["route_sha256"], pricing_evidence_sha256=route["pricing_evidence_sha256"])
        route["request_budget_mechanism_sha256"] = digest(mechanism)
        paid_state["spec"] = {"schema": "ukrainian-llm-eval.request-budget-route.v2", "mechanism": mechanism}
    route["operator_authorization_sha256"] = digest(auth)
    source = "import json, sys\nfrom datetime import datetime, timezone\nr=json.load(sys.stdin)\n"
    source += "assert set(r) == {'schema','nonce','requested_at','route_sha256','model','effort','condition','composite_sha256','requirements','request_sha256'}\n"
    source += "assert r['requirements']['max_output_tokens'] == 100\n"
    source += "result=json.loads(" + repr(json.dumps(claims)) + ")\n"
    if fail_large:
        source += "result['capability']['observed']['input_fits'] = r['requirements']['input_utf8_bytes'] < 1500\n"
    source += "result.update(nonce=r['nonce'], request_sha256=r['request_sha256'], observed_at=datetime.now(timezone.utc).isoformat())\nprint(json.dumps(result))\n"
    script, lock = _fixture(tmp_path, source)
    spec = _command_spec(script, lock, stdout_max_bytes=8192)
    route["admission_command_sha256"] = command_identity_sha256(spec)
    manifest = build_experiment_manifest(manifest["protocol_sha256"], manifest["suites"], [route],
                                         scorer_sha256=manifest["scorer_sha256"],
                                         tool_policy_sha256=manifest["tool_policy_sha256"],
                                         **({"spending_policy": policy, "new_spend_cap_micro_usd": 1000} if policy else {}))
    return ((packets, segments, manifest, build_execution_plan(manifest), configs),
            CommandAdmissionController({"fixture": spec}, {"fixture": auth}))


def write(path, value):
    path.write_text(json.dumps(value))
    return path


def cli_args(tmp_path, args, controller):
    packets, segments, manifest, plan, configs = args
    runtime = {"schema": "ukrainian-llm-eval.research-runtime-inputs.v1"}
    for field, values in (("packets", packets), ("segment_plans", segments), ("configs", configs)):
        runtime[field] = {}
        for name, value in values.items():
            path = write(tmp_path / f"{field}-{name}.json", value)
            runtime[field][name] = path.name
    maps = {}
    for field, schema, values in (
        ("admission-specs", "research-admission-specs", controller.specs),
        ("operator-authorizations", "research-operator-authorizations", controller.authorizations),
    ):
        routes = {key: write(tmp_path / f"{field}-{key}.json", value).name for key, value in values.items()}
        maps[field] = write(tmp_path / f"{field}.json", {"schema": f"ukrainian-llm-eval.{schema}.v1", "routes": routes})
    maps.update(inputs=write(tmp_path / "runtime.json", runtime),
                manifest=write(tmp_path / "manifest.json", manifest),
                **{"execution-plan": write(tmp_path / "plan.json", plan), "execution-root": tmp_path / "execution",
                   "evidence-root": tmp_path / "observation", "sources-urls": write(tmp_path / "sources.json", {"fixture": SOURCES})})
    return ["check-research", *[str(item) for pair in maps.items() for item in ("--" + pair[0], pair[1])]]


def forbidden(*args, **kwargs):
    pytest.fail("candidate/adapter execution is unreachable from readiness")


def test_complete_observation_validates_all_segments_no_execution(monkeypatch, tmp_path):
    args, controller = fixture(tmp_path)
    for module, name in ((execution, "run_exam"), (scheduling, "execute_attempt"),
                         (adapters, "preflight"), (scheduling, "run_research")):
        monkeypatch.setattr(module, name, forbidden)
    seen = []
    original = readiness.derive_segment_packet

    def derive(*values, **kwargs):
        seen.append(kwargs["segment_id"])
        return original(*values, **kwargs)

    monkeypatch.setattr(readiness, "derive_segment_packet", derive)
    root = tmp_path / "execution"
    root.mkdir(mode=0o700)
    # Interrupted scored attempts must retain their original state and IDs.
    attempt = EvidenceStore(root / "evidence").start({"denominator": 1}, attempt_id=args[3]["cells"][0]["segments"][0]["attempt_id"])
    attempt.append("started", {"status": "pending"})
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = readiness.check_research(*args, root, evidence_root=tmp_path / "observation",
                                     admission_probe=controller, sources_urls={"fixture": SOURCES})
    assert result["status"] == "ok"
    assert result["cells"] == result["representative_probes"] == 6
    assert result["structural_segments"] == len(seen) == 12
    assert len(set(seen)) == 2
    assert result["execution_admitted"] is False
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    receipts = EvidenceStore(tmp_path / "observation/admission-evidence").verify_all()
    assert len({r["metadata"]["request_sha256"] for r in receipts.values()}) == 6
    assert {r["metadata"]["execution_binding"]["segment_id"] for r in receipts.values()} == {seen[1]}
    assert "Виберіть" not in "".join(p.read_text() for p in (tmp_path / "observation").rglob("*.jsonl"))


@pytest.mark.parametrize("change", ["packet", "segments", "largest-segment", "config", "command", "authorization", "tool-policy", "suite", "condition", "tools"])
def test_drift_or_missing_bindings_fail_before_probes(tmp_path, change):
    args, controller = fixture(tmp_path)
    packets, segments, manifest, plan, configs = args
    if change == "packet": packets.clear()
    elif change in {"segments", "largest-segment"}:
        segments["ulp"]["segments"][0 if change == "segments" else 1]["item_ids"] = ["foreign"]
    elif change == "config": configs["fixture"]["model"] = "other"
    elif change == "command": controller.specs.clear()
    elif change == "authorization": controller.authorizations["fixture"]["allow_paid"] = True
    elif change == "tool-policy": manifest["tool_policy_sha256"] = "7" * 64
    elif change == "suite": manifest["suites"][0]["suite_id"] = "foreign"
    elif change == "condition": plan["cells"][0]["condition"] = "unknown"
    else:
        configs["fixture"]["tools"] = ["untrusted_tool"]
        manifest["routes"][0]["config_sha256"] = digest(configs["fixture"])
        manifest = build_experiment_manifest(manifest["protocol_sha256"], manifest["suites"], manifest["routes"],
                                             scorer_sha256=manifest["scorer_sha256"],
                                             tool_policy_sha256=manifest["tool_policy_sha256"])
        args = (packets, segments, manifest, build_execution_plan(manifest), configs)
    with pytest.raises(ValueError):
        readiness.check_research(*args, tmp_path / "execution", evidence_root=tmp_path / "observation",
                                 admission_probe=controller, sources_urls={"fixture": SOURCES})
    assert not (tmp_path / "execution").exists() and not (tmp_path / "observation").exists()


def test_largest_profile_failure_preserves_every_probe(tmp_path):
    args, controller = fixture(tmp_path, fail_large=True)
    result = readiness.check_research(*args, tmp_path / "execution", evidence_root=tmp_path / "observation",
                                     admission_probe=controller, sources_urls={"fixture": SOURCES})
    assert result["status"] == "failed" and result["failed_checks"] == 6
    receipts = EvidenceStore(tmp_path / "observation/admission-evidence").verify_all()
    assert len(receipts) == 6 and all(r["result"]["status"] == "failed" for r in receipts.values())
    assert not (tmp_path / "execution").exists()


def test_cli_installed_entrypoint_outside_checkout_and_no_execution_switch(tmp_path):
    args, controller = fixture(tmp_path)
    argv = cli_args(tmp_path, args, controller)
    executable = Path(sys.executable).parent / "ukrainian-llm-eval"
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run([str(executable), *argv], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["representative_probes"] == 6
    assert not (tmp_path / "execution").exists()
    for option in ("--resume", "--execute", "--reset-ledger"):
        rejected = subprocess.run([str(executable), *argv, option], cwd=tmp_path, env=environment,
                                  capture_output=True, text=True, check=False)
        assert rejected.returncode == 2


def test_cli_rejects_missing_authorization_map_before_any_probe(tmp_path):
    args, controller = fixture(tmp_path)
    argv = cli_args(tmp_path, args, controller)
    write(tmp_path / "operator-authorizations.json", {
        "schema": "ukrainian-llm-eval.research-operator-authorizations.v1", "routes": {}})
    from ukrainian_llm_eval.__main__ import execute, parser

    with pytest.raises(ValueError):
        execute(parser().parse_args(argv))
    assert not (tmp_path / "observation").exists()


def test_existing_stop_and_overlapping_observation_rejected_without_changes(tmp_path):
    args, controller = fixture(tmp_path)
    root = tmp_path / "execution"
    root.mkdir()
    write(root / "stop.json", {"reason": "budget"})
    with pytest.raises(ExamError, match="retained stop"):
        readiness.check_research(*args, root, evidence_root=tmp_path / "observation",
                                 admission_probe=controller, sources_urls={"fixture": SOURCES})
    with pytest.raises(ExamError, match="separate"):
        readiness.check_research(*args, root, evidence_root=root / "observation",
                                 admission_probe=controller, sources_urls={"fixture": SOURCES})
    assert list(root.iterdir()) == [root / "stop.json"]


@pytest.mark.parametrize("sufficient", [True, False])
def test_cli_existing_shared_ledger_is_unchanged_and_only_next_capacity_required(tmp_path, sufficient, monkeypatch):
    from test_readiness_budget import tree_state

    from ukrainian_llm_eval.__main__ import execute, parser
    from ukrainian_llm_eval.request_budget import RequestBudgetController
    from ukrainian_llm_eval.spending_ledger import SharedSpendingLedger

    monkeypatch.delenv("FIXTURE_ENDPOINT", raising=False)
    paid = {}
    args, controller = fixture(tmp_path, paid_state=paid)
    if sufficient:
        paid["ledger"].settle("reserve-provider-bound", charged_micro_usd=394, evidence_sha256="a" * 64)
    argv = cli_args(tmp_path, args, controller)
    spec_path = write(tmp_path / "budget-route.json", paid["spec"])
    budget_map = write(tmp_path / "budgets.json", {
        "schema": "ukrainian-llm-eval.research-request-budgets.v1", "routes": {"fixture": spec_path.name}})
    argv += ["--request-budgets", str(budget_map), "--shared-spending-ledger", str(paid["ledger_path"])]
    before = tree_state(tmp_path / "prior")
    for method in ("bind", "for_attempt"):
        monkeypatch.setattr(RequestBudgetController, method, forbidden)
    for method in ("__init__", "reserve", "settle", "_initialize"):
        monkeypatch.setattr(SharedSpendingLedger, method, forbidden)
    monkeypatch.setattr(execution, "run_exam", forbidden)
    assert args[3]["reservation_total_micro_usd"] > 1000
    assert execute(parser().parse_args(argv)) == (0 if sufficient else 2)
    assert tree_state(tmp_path / "prior") == before
    assert not (tmp_path / "execution").exists()
    receipts = EvidenceStore(tmp_path / "observation/admission-evidence").verify_all()
    assert len(receipts) == 6
