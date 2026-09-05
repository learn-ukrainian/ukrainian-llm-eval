import json

import pytest

from ukrainian_llm_eval import execution, scheduling
from ukrainian_llm_eval.benchmark_manifest import build_execution_plan, build_experiment_manifest
from ukrainian_llm_eval.core import ExamError, digest
from ukrainian_llm_eval.evidence import EvidenceStore
from ukrainian_llm_eval.request_budget import request_budget_attempt_id
from ukrainian_llm_eval.segmentation import derive_segment_plan


def inputs(*, metered=False):
    body = {"schema": "zno-nmt.questions.v1", "items": [
        {"id": f"q{i:04d}", "kind": "single", "question": f"Виберіть {i}",
         "options": [{"id": "A", "text": "так"}, {"id": "B", "text": "ні"}], "rows": []}
        for i in (1, 2)]}
    packet = {**body, "packet_sha256": digest(body)}
    config = {"schema": "zno-nmt.config.v1", "adapter": "claude", "model": "fixture", "effort": None,
              "timeout_seconds": 10, "max_output_tokens": 100, "max_tool_calls": 2,
              "repeats": 3, "tools": [], "corpus_id": None}
    segmentation = derive_segment_plan(packet, suite_id="ulp", protocol_sha256="a" * 64,
                                       denominator={"items": 2, "points": 2})
    suite = {"suite_id": "ulp", "source_sha256": "b" * 64, "profile_sha256": "c" * 64, "key_sha256": "8" * 64,
             "segment_plan": segmentation, "limits": {"timeout_seconds": 10, "max_output_tokens": 100,
                                                       "max_tool_calls": 2}}
    route = {"route_id": "fixture", "route_sha256": execution.route_fingerprint(config, None),
             "config_sha256": digest(config), "conditions": ["closed-book", "sources"],
             "unsupported_condition_evidence": {}, "capability_evidence_sha256": "d" * 64,
             "pricing_evidence_sha256": "e" * 64, "entitlement_evidence_sha256": "f" * 64,
             "admission_command_sha256": "1" * 64, "operator_authorization_sha256": "2" * 64,
             "request_budget_mechanism_sha256": "3" * 64 if metered else None,
             "billing": {"kind": "metered" if metered else "verified_subscription", "units": "tokens",
                         "input_micro_usd_per_million_tokens": 1000 if metered else 0,
                         "output_micro_usd_per_million_tokens": 1000 if metered else 0,
                         "max_total_input_tokens": 10000, "max_total_output_tokens": 300,
                         "max_tool_rounds": 2, "tool_round_micro_usd": 0}}
    manifest = build_experiment_manifest("a" * 64, [suite], [route], scorer_sha256="9" * 64,
                                         tool_policy_sha256=scheduling.research_implementation_sha256())
    plan = build_execution_plan(manifest)
    return {"ulp": packet}, {"ulp": segmentation}, manifest, plan, {"fixture": config}


@pytest.mark.parametrize("missing_controller", [True, False])
def test_subscription_frozen_budget_cannot_start_without_budget(monkeypatch, tmp_path, missing_controller):
    packets, plans, manifest, _, configs = inputs()
    manifest["routes"][0]["request_budget_mechanism_sha256"] = "7" * 64
    manifest = build_experiment_manifest(
        manifest["protocol_sha256"], manifest["suites"], manifest["routes"],
        scorer_sha256=manifest["scorer_sha256"], tool_policy_sha256=manifest["tool_policy_sha256"])
    plan = build_execution_plan(manifest)
    calls = []
    monkeypatch.setattr(execution, "run_exam", trial(calls))
    root = tmp_path / "research"

    class MissingBudget(FixtureBudgets):
        def for_attempt(self, *_args):
            return None

    run = scheduling.run_research(packets, plans, manifest, plan, configs, root,
                                  admission_probe=admit,
                                  request_budget_controller=None if missing_controller else MissingBudget())
    if missing_controller:
        with pytest.raises(ExamError, match="frozen request-budget mechanism"):
            list(run)
        assert not root.exists()
    else:
        assert list(run) == [{"status": "stopped", "reason": "admission_failed"}]
        assert EvidenceStore(root / "admission-evidence").verify_all()
        assert not EvidenceStore(root / "evidence").verify_all()
    assert calls == []


def admit(route, _config, _condition, *, request, execution_binding, reserved_micro_usd,
          remaining_ceiling_micro_usd, evidence_dir):
    # Synthetic controller fixture; never an eligible live provider probe.
    receipt = {"schema": "ukrainian-llm-eval.admission-receipt.v1", "request_sha256": request["request_sha256"],
               "result_sha256": "a" * 64, "composite_sha256": request["composite_sha256"],
               "operator_authorization_sha256": route["operator_authorization_sha256"],
               "incremental_segment_cost_micro_usd": reserved_micro_usd,
               "credit_available_micro_usd": None,
               "account_sha256": "4" * 64,
               **{name + "_state_sha256": route[name + "_evidence_sha256"] for name in ("pricing", "entitlement", "capability")}}
    attempt = EvidenceStore(evidence_dir).start({"denominator": 0, "request_sha256": request["request_sha256"],
                                                "route_sha256": route["route_sha256"], "command_sha256": route["admission_command_sha256"],
                                                "composite_sha256": request["composite_sha256"],
                                                "execution_binding": execution_binding})
    return receipt, attempt.finalize(receipt)


admit.prepare = lambda _manifest, _plan: None


class FixtureBudgets:
    """Scheduler fixture; exact request controls have process-backed tests."""

    def prepare(self, _manifest, _plan):
        return None

    def bind(self, _root):
        self.store = EvidenceStore(_root / "request-budget-evidence")
        for budget_id, evidence in self.store.verify_all().items():
            if not evidence["complete"]:
                self.store.finalize(
                    budget_id,
                    {"schema": "ukrainian-llm-eval.request-budget-interruption.v1", "status": "interrupted",
                     "credit_commitment_micro_usd": evidence["metadata"]["credit_commitment_micro_usd"]},
                    status="interrupted",
                )

    def for_attempt(self, route, config, attempt_id, admission_receipt):
        if route["request_budget_mechanism_sha256"] is None:
            return None
        billing = route["billing"]
        maximum = ((billing["input_micro_usd_per_million_tokens"] * billing["max_total_input_tokens"] + 999_999) // 1_000_000
                   + (billing["output_micro_usd_per_million_tokens"] * billing["max_total_output_tokens"] + 999_999) // 1_000_000
                   + billing["max_tool_rounds"] * billing["tool_round_micro_usd"])
        attempt = self.store.start(
            {"denominator": 0, "candidate_attempt_id": attempt_id, "route_sha256": route["route_sha256"],
             "account_sha256": admission_receipt["account_sha256"],
             "mechanism_sha256": route["request_budget_mechanism_sha256"],
             "billing_kind": billing["kind"], "credit_commitment_micro_usd": 0,
             "maximum_segment_charge_micro_usd": maximum},
            attempt_id=request_budget_attempt_id(attempt_id),
        )

        class Budget:
            def finalize(self, status):
                result = {"schema": "ukrainian-llm-eval.request-budget-receipt.v1", "status": status,
                          "mechanism_sha256": route["request_budget_mechanism_sha256"],
                          "rounds_committed": 0, "input_tokens_committed": 0,
                          "output_tokens_reserved": 0, "input_tokens_observed": 0,
                          "output_tokens_including_reasoning_observed": 0, "tool_calls_observed": 0,
                          "formula_charge_micro_usd": 0, "provider_reported_charge_micro_usd": None,
                          "reported_cost_kind": None}
                return attempt.finalize(result, status=status)

        return Budget()


fixture_budgets = FixtureBudgets()


def trial(calls, *, interrupt_at=None, failed_at=None, cost=None):
    def execute(packet, config, condition, *, evidence, **kwargs):
        calls.append((packet, condition))
        evidence("prompt", {"packet": packet})
        if len(calls) == interrupt_at:
            raise KeyboardInterrupt
        result = {"schema": "zno-nmt.run.v1", "packet_sha256": packet["packet_sha256"],
                "status": "failed" if len(calls) == failed_at else "ok",
                "responses": {item["id"]: "A" for item in packet["items"]},
                "identity": {"model": config["model"], "effective_effort": "unknown",
                             "session_id": f"session-{len(calls)}"},
                "metrics": {"tool_calls": 0, "cost_usd": cost}}
        budget = kwargs.get("request_budget")
        if budget is not None:
            receipt = budget.finalize("failed" if result["status"] == "failed" else "completed")
            result["identity"]["request_budget_receipt_sha256"] = digest(receipt)
        return result
    return execute


def test_complete_schedule_preserves_full_cells_and_resume_never_repeats(monkeypatch, tmp_path):
    args = inputs()
    calls = []
    monkeypatch.setattr(execution, "run_exam", trial(calls))
    root = tmp_path / "research"
    progress = list(scheduling.run_research(*args, root, admission_probe=admit,
                                             request_budget_controller=fixture_budgets))
    assert len(progress) == 6 and all(item["status"] == "ok" for item in progress)
    assert len(calls) == 12 and all(len(packet["items"]) == 1 for packet, _ in calls)
    assert [condition for _, condition in calls] == ["closed-book"] * 2 + ["sources"] * 4 + ["closed-book"] * 4 + ["sources"] * 2
    before = {p.name: p.read_bytes() for p in root.glob("*.json")}
    assert list(scheduling.run_research(*args, root, admission_probe=admit,
                                        request_budget_controller=fixture_budgets, resume=True)) == progress
    assert len(calls) == 12
    assert {p.name: p.read_bytes() for p in root.glob("*.json")} == before
    report = json.loads((root / "result-manifest.json").read_text())
    assert report["cells_complete"] == report["cells_required"] == 6
    for cell in args[3]["cells"]:
        assert json.loads((root / (cell["cell_id"] + ".json")).read_text())["responses"] == {"q0001": "A", "q0002": "A"}


@pytest.mark.parametrize("interrupted", [False, True])
def test_failure_stops_cell_and_preserves_attempt_without_retry(monkeypatch, tmp_path, interrupted):
    args = inputs(metered=True)
    calls = []
    monkeypatch.setattr(execution, "run_exam", trial(calls, interrupt_at=1 if interrupted else None,
                                                    failed_at=None if interrupted else 1))
    root = tmp_path / "research"
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            list(scheduling.run_research(*args, root, admission_probe=admit,
                                         request_budget_controller=fixture_budgets))
        first = EvidenceStore(root / "evidence").verify_all()
        assert len(first) == 1 and next(iter(first.values()))["complete"] is False
        outcomes = list(scheduling.run_research(*args, root, admission_probe=admit,
                                                request_budget_controller=fixture_budgets, resume=True))
    else:
        outcomes = list(scheduling.run_research(*args, root, admission_probe=admit,
                                                request_budget_controller=fixture_budgets))
    assert len(calls) == 11 and outcomes[0]["status"] == "failed"
    assert all(item["status"] == "ok" for item in outcomes[1:])
    first_cell = args[3]["cells"][0]
    assert first_cell["segments"][1]["attempt_id"] not in EvidenceStore(root / "evidence").verify_all()
    result = json.loads((root / (first_cell["cell_id"] + ".json")).read_text())
    assert result["responses"] is None
    assert result["reserved_micro_usd_started"] == first_cell["segments"][0]["reserved_micro_usd"]
    list(scheduling.run_research(*args, root, admission_probe=admit,
                                 request_budget_controller=fixture_budgets, resume=True))
    assert len(calls) == 11


def test_admission_drift_stops_before_allocating_or_calling(monkeypatch, tmp_path):
    args = inputs()
    monkeypatch.setattr(execution, "run_exam", lambda *a, **kw: pytest.fail("provider called"))
    root = tmp_path / "research"
    def rejected(*a, **kw):
        raise ExamError("synthetic admission rejection")
    rejected.prepare = admit.prepare
    assert list(scheduling.run_research(*args, root, admission_probe=rejected,
                                        request_budget_controller=fixture_budgets)) == [
        {"status": "stopped", "reason": "admission_failed"}]
    assert EvidenceStore(root / "evidence").verify_all() == {}
    assert next(scheduling.run_research(*args, root, admission_probe=admit,
                                        request_budget_controller=fixture_budgets, resume=True))["status"] == "stopped"


def test_cost_overrun_stops_entire_experiment_with_original_evidence(monkeypatch, tmp_path):
    args = inputs(metered=True)
    calls = []
    monkeypatch.setattr(execution, "run_exam", trial(calls, cost=11))
    root = tmp_path / "research"
    assert list(scheduling.run_research(*args, root, admission_probe=admit,
                                        request_budget_controller=fixture_budgets)) == [
        {"status": "stopped", "reason": "cost_reservation_overrun"}]
    assert len(calls) == 1
    receipt = next(iter(EvidenceStore(root / "evidence").verify_all().values()))
    assert receipt["result"]["metrics"]["cost_usd"] == 11
    assert not (root / "result-manifest.json").exists()


def test_endpoint_drift_rejected_before_creating_directory(tmp_path):
    args = inputs()
    with pytest.raises(ExamError, match="endpoint drift"):
        list(scheduling.run_research(*args, tmp_path / "research", admission_probe=admit,
                                    request_budget_controller=fixture_budgets,
                                    sources_urls={"fixture": "https://different.invalid/mcp"}))
    assert not (tmp_path / "research").exists()


def test_concurrent_executor_cannot_claim_second_reservation(monkeypatch, tmp_path):
    args = inputs()
    calls = []
    root = tmp_path / "research"
    ordinary = trial(calls)

    def nested(*values, **kwargs):
        with pytest.raises(ExamError, match="already running"):
            list(scheduling.run_research(*args, root, admission_probe=admit,
                                         request_budget_controller=fixture_budgets, resume=True))
        return ordinary(*values, **kwargs)

    monkeypatch.setattr(execution, "run_exam", nested)
    list(scheduling.run_research(*args, root, admission_probe=admit,
                                 request_budget_controller=fixture_budgets))
    assert len(calls) == 12


@pytest.mark.parametrize(("change", "reason"), [
    ({"identity": {"model": "other", "session_id": "one"}}, "model_identity_drift"),
    ({"metrics": {"tool_calls": 3}}, "tool_budget_overrun"),
    ({"metrics": {"input_tokens": 10001}}, "token_reservation_overrun"),
])
@pytest.mark.parametrize("metered", [True, False])
def test_observed_drift_stops_with_evidence(monkeypatch, tmp_path, change, reason, metered):
    args = inputs(metered=metered)
    calls = []
    ordinary = trial(calls)
    def changed(*values, **kwargs):
        result = ordinary(*values, **kwargs)
        if "identity" in change:
            result["identity"].update(change["identity"])
        else:
            result.update(change)
        return result
    monkeypatch.setattr(execution, "run_exam", changed)
    root = tmp_path / "research"
    assert list(scheduling.run_research(*args, root, admission_probe=admit,
                                        request_budget_controller=fixture_budgets)) == [
                                            {"status": "stopped", "reason": reason}]
    assert len(calls) == 1
    assert len(EvidenceStore(root / "evidence").verify_all()) == 1


def test_reused_session_stops_before_second_cell_score(monkeypatch, tmp_path):
    args = inputs()
    calls = []
    ordinary = trial(calls)

    def reused(*a, **kw):
        result = ordinary(*a, **kw)
        result["identity"]["session_id"] = "reused"
        return result

    monkeypatch.setattr(execution, "run_exam", reused)
    root = tmp_path / "research"
    assert list(scheduling.run_research(*args, root, admission_probe=admit,
                                        request_budget_controller=fixture_budgets)) == [
        {"status": "stopped", "reason": "missing_or_reused_session"}]
    assert len(calls) == 2 and not list(root.glob("cell-*.json"))
