import json

import pytest
from test_admission_command import _command_spec, _fixture

from ukrainian_llm_eval import adapters
from ukrainian_llm_eval.admission_command import command_identity_sha256
from ukrainian_llm_eval.core import ExamError, digest
from ukrainian_llm_eval.evidence import EvidenceStore
from ukrainian_llm_eval.request_budget import (
    CACHE_BILLING,
    COUNTER_RESULT_SCHEMA,
    INPUT_SEMANTICS,
    MECHANISM_SCHEMA,
    PROVIDER_BOUND_MECHANISM_SCHEMA,
    PROVIDER_BOUND_ROUTE_SPEC_SCHEMA,
    ROUTE_SPEC_SCHEMA,
    SERIALIZER,
    USAGE_BOUND_MECHANISM_SCHEMA,
    USAGE_BOUND_ROUTE_SPEC_SCHEMA,
    RequestBudgetController,
    RequestBudgetError,
    validate_mechanism,
)
from ukrainian_llm_eval.spending_ledger import SharedSpendingLedger, SpendingCapExceeded


def _packet():
    body = {
        "schema": "zno-nmt.questions.v1",
        "items": [
            {
                "id": "q0001",
                "kind": "single",
                "question": "Оберіть.",
                "options": [{"id": "A", "text": "так"}, {"id": "B", "text": "ні"}],
                "rows": [],
            }
        ],
    }
    return {**body, "packet_sha256": digest(body)}


def _counter(tmp_path):
    semantics = "a" * 64
    source = (
        "import hashlib,json,sys\n"
        "raw=sys.stdin.buffer.read()\n"
        "json.loads(raw)\n"
        f"print(json.dumps({{'schema':{COUNTER_RESULT_SCHEMA!r},'request_sha256':hashlib.sha256(raw).hexdigest(),"
        f"'counter_semantics_sha256':{semantics!r},'input_tokens':len(raw)}}))\n"
    )
    script, lock = _fixture(tmp_path, source)
    spec = _command_spec(script, lock, stdout_max_bytes=8192)
    return spec, semantics


def _values(tmp_path, *, kind="metered", credit=None, max_input=100_000, max_output=300, max_result=4096):
    command, semantics = _counter(tmp_path)
    route_sha = "b" * 64
    mechanism = {
        "schema": MECHANISM_SCHEMA,
        "route_sha256": route_sha,
        "provider": "fixture-provider",
        "serializer": SERIALIZER,
        "counter_command_sha256": command_identity_sha256(command),
        "counter_semantics_sha256": semantics,
        "input_semantics": INPUT_SEMANTICS,
        "output_parameter": {
            "name": "max_tokens",
            "includes_reasoning": True,
            "includes_tool_calls": True,
            "includes_final_output": True,
        },
        "usage": {
            "input_tokens_path": ["prompt_tokens"],
            "output_tokens_path": ["completion_tokens"],
            "output_includes_reasoning": True,
            "reported_cost_path": ["cost"],
            "reported_cost_kind": "account_charge" if kind != "verified_subscription" else "nonincremental_estimate",
            "reported_cost_scope": "request",
        },
        "cache_billing": CACHE_BILLING,
        "max_tool_result_utf8_bytes": max_result,
    }
    rate = 1_000_000
    route = {
        "route_id": "fixture",
        "route_sha256": route_sha,
        "request_budget_mechanism_sha256": digest(mechanism),
        "billing": {
            "kind": kind,
            "input_micro_usd_per_million_tokens": rate,
            "output_micro_usd_per_million_tokens": rate,
            "max_total_input_tokens": max_input,
            "max_total_output_tokens": max_output,
            "max_tool_rounds": 2,
            "tool_round_micro_usd": 3,
        },
    }
    config = {
        "schema": "zno-nmt.config.v1",
        "adapter": "chat-http",
        "provider": "fixture-provider",
        "model": "fixture-model",
        "effort": "high",
        "timeout_seconds": 10,
        "max_output_tokens": 100,
        "max_tool_calls": 2,
        "repeats": 3,
        "tools": ["verify_word"],
        "corpus_id": "fixture-corpus",
        "endpoint_env": "FIXTURE_ENDPOINT",
        "key_env": None,
    }
    route_spec = {"schema": ROUTE_SPEC_SCHEMA, "mechanism": mechanism, "counter_command": command}
    controller = RequestBudgetController({"fixture": route_spec})
    controller.prepare({"routes": [route]}, {"cells": []})
    (tmp_path / "research").mkdir(mode=0o700)
    controller.bind(tmp_path / "research")
    admission = {"credit_available_micro_usd": credit, "account_sha256": "c" * 64}
    budget = controller.for_attempt(route, config, "attempt-one", admission)
    assert budget is not None
    return route, config, mechanism, controller, budget


def test_http_sends_exact_counted_bytes_with_schema_tools_and_full_tool_history(monkeypatch, tmp_path):
    _route, config, _mechanism, _controller, budget = _values(tmp_path)
    monkeypatch.setenv("FIXTURE_ENDPOINT", "https://provider.invalid/chat")
    monkeypatch.setattr(
        adapters,
        "_mcp_list_tools",
        lambda *_args: ([{"name": "verify_word", "inputSchema": {"type": "object"}}], "server"),
    )
    monkeypatch.setattr(adapters, "_mcp_call", lambda *_args: {"word": "слово", "valid": True})
    sent = []

    def response(_url, payload, **_kwargs):
        assert isinstance(payload, bytes)
        sent.append(payload)
        common = {"model": "fixture-model", "usage": {"prompt_tokens": len(payload), "completion_tokens": 5,
                                                        "cost": "0.000005"}}
        if len(sent) == 1:
            return common | {
                "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                             "tool_calls": [{"id": "call-1", "type": "function", "function": {
                                 "name": "mcp__sources__verify_word", "arguments": "{\"word\":\"слово\"}"}}]}}]
            }
        return common | {
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{\"responses\":{\"q0001\":\"A\"}}"}}]
        }

    monkeypatch.setattr(adapters, "_http_json", response)
    result = adapters.run_chat_http(
        _packet(), config, "sources", sources_url="https://sources.invalid/mcp",
        prompt="Кандидатський запит", request_budget=budget,
    )
    receipt = budget.finalize("completed")

    assert result["responses"] == {"q0001": "A"}
    assert result["metrics"]["cost_usd"] == 0.00001
    assert len(sent) == 2
    first, second = map(json.loads, sent)
    assert first["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert first["response_format"]["json_schema"]["schema"]["required"] == ["responses"]
    assert second["messages"][1]["tool_calls"][0]["function"]["arguments"] == "{\"word\":\"слово\"}"
    assert json.loads(second["messages"][2]["content"]) == {"valid": True, "word": "слово"}
    evidence = EvidenceStore(tmp_path / "research" / "request-budget-evidence").verify(receipt["attempt_id"])
    assert evidence["result"]["rounds_committed"] == 2
    assert evidence["result"]["input_tokens_committed"] == sum(len(item) for item in sent)


def test_counter_time_is_charged_to_total_deadline_before_paid_transport(monkeypatch, tmp_path):
    _route, config, _mechanism, _controller, budget = _values(tmp_path)
    monkeypatch.setenv("FIXTURE_ENDPOINT", "https://provider.invalid/chat")
    now = [100.0]
    monkeypatch.setattr(adapters.time, "monotonic", lambda: now[0])
    commit_request = budget.commit_request

    def slow_commit(payload):
        committed = commit_request(payload)
        now[0] = 111.0
        return committed

    monkeypatch.setattr(budget, "commit_request", slow_commit)
    monkeypatch.setattr(
        adapters,
        "_http_json",
        lambda *_args, **_kwargs: pytest.fail("expired paid request reached transport"),
    )

    with pytest.raises(adapters.AdapterError, match="HTTP total timeout"):
        adapters.run_chat_http(
            _packet(), config, "closed-book", sources_url=None,
            prompt="Кандидатський запит", request_budget=budget,
        )
    assert budget.rounds == 1


def test_second_round_is_rejected_before_transport_when_cumulative_history_exceeds_input_cap(tmp_path):
    _route, _config, _mechanism, _controller, budget = _values(tmp_path, max_input=900)
    first = {"model": "fixture-model", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    raw, _ = budget.commit_request(first)
    budget.observe({"prompt_tokens": len(raw), "completion_tokens": 1, "cost": "0.000001"}, tool_calls=1)
    second = {"model": "fixture-model", "messages": first["messages"] + [
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "tool", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "x" * 800},
    ], "max_tokens": 100}
    with pytest.raises(RequestBudgetError, match="cumulative input"):
        budget.commit_request(second)


def test_existing_credit_rejects_stale_balance_before_second_candidate(tmp_path):
    route, config, _mechanism, controller, _budget = _values(tmp_path, kind="existing_credit", credit=100_306)
    with pytest.raises(ExamError, match="retained request commitments"):
        controller.for_attempt(
            route, config, "attempt-two",
            {"credit_available_micro_usd": 100_306, "account_sha256": "c" * 64},
        )
    stored = EvidenceStore(tmp_path / "research" / "request-budget-evidence").verify_all()
    assert len(stored) == 1
    assert next(iter(stored.values()))["metadata"]["credit_commitment_micro_usd"] == 100_306


def test_same_controller_resume_preserves_orphan_commitment_and_never_retries(tmp_path):
    route, config, _mechanism, controller, _budget = _values(
        tmp_path, kind="existing_credit", credit=200_612
    )
    manifest = {"routes": [route]}
    controller.prepare(manifest, {"cells": []})
    controller.bind(tmp_path / "research")
    stored = EvidenceStore(tmp_path / "research" / "request-budget-evidence").verify_all()
    assert len(stored) == 1
    assert next(iter(stored.values()))["terminal_status"] == "interrupted"
    with pytest.raises(ExamError, match="already exists"):
        controller.for_attempt(
            route, config, "attempt-one",
            {"credit_available_micro_usd": 200_612, "account_sha256": "c" * 64},
        )
    with pytest.raises(ExamError, match="retained request commitments"):
        controller.for_attempt(
            route, config, "attempt-two",
            {"credit_available_micro_usd": 100_306, "account_sha256": "c" * 64},
        )


def test_existing_credit_commitments_follow_account_across_distinct_routes(tmp_path):
    route, config, _mechanism, controller, _budget = _values(
        tmp_path, kind="existing_credit", credit=200_612
    )
    first_spec = controller._raw_route_specs["fixture"]
    second_route = json.loads(json.dumps(route))
    second_route.update(route_id="fixture-two", route_sha256="d" * 64)
    second_spec = json.loads(json.dumps(first_spec))
    second_spec["counter_command"] = first_spec["counter_command"]
    second_spec["mechanism"]["route_sha256"] = second_route["route_sha256"]
    second_route["request_budget_mechanism_sha256"] = digest(second_spec["mechanism"])
    combined = RequestBudgetController({"fixture": first_spec, "fixture-two": second_spec})
    combined.prepare({"routes": [route, second_route]}, {"cells": []})
    combined.bind(tmp_path / "research")
    with pytest.raises(ExamError, match="retained request commitments"):
        combined.for_attempt(
            second_route,
            config | {"provider": "fixture-provider"},
            "attempt-two",
            {"credit_available_micro_usd": 100_306, "account_sha256": "c" * 64},
        )


def test_unknown_reasoning_accounting_and_oversized_tool_result_fail_closed(tmp_path):
    _route, _config, mechanism, _controller, budget = _values(tmp_path, max_result=8)
    unknown = dict(mechanism)
    unknown["output_parameter"] = dict(mechanism["output_parameter"], includes_reasoning=False)
    with pytest.raises(RequestBudgetError, match="reasoning-inclusive"):
        validate_mechanism(unknown)
    with pytest.raises(RequestBudgetError, match="tool result exceeds"):
        budget.serialize_tool_result({"content": "0123456789"})


def test_observed_reasoning_inclusive_output_overrun_stops_budget(tmp_path):
    _route, _config, _mechanism, _controller, budget = _values(tmp_path)
    payload = {"model": "fixture-model", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    raw, _ = budget.commit_request(payload)
    with pytest.raises(RequestBudgetError, match="reasoning-inclusive output"):
        budget.observe({"prompt_tokens": len(raw), "completion_tokens": 101, "cost": "0.000001"}, tool_calls=0)


def test_provider_reported_charge_overrun_is_distinct_from_formula(tmp_path):
    _route, _config, _mechanism, _controller, budget = _values(tmp_path)
    payload = {"model": "fixture-model", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    raw, _ = budget.commit_request(payload)
    with pytest.raises(RequestBudgetError, match="provider-reported charge"):
        budget.observe({"prompt_tokens": len(raw), "completion_tokens": 1, "cost": "1.00"}, tool_calls=0)


def test_provider_reported_request_charges_are_cumulative(tmp_path):
    _route, _config, _mechanism, _controller, budget = _values(tmp_path)
    first = {"model": "fixture-model", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    raw, _ = budget.commit_request(first)
    budget.observe({"prompt_tokens": len(raw), "completion_tokens": 1, "cost": "0.06"}, tool_calls=1)
    second = {"model": "fixture-model", "messages": first["messages"] + [
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "tool", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
    ], "max_tokens": 100}
    raw, _ = budget.commit_request(second)
    with pytest.raises(RequestBudgetError, match="provider-reported charge"):
        budget.observe({"prompt_tokens": len(raw), "completion_tokens": 1, "cost": "0.06"}, tool_calls=0)


def test_paid_controller_is_required_before_research_root_creation(tmp_path):
    from test_research_scheduling import admit, inputs

    from ukrainian_llm_eval import scheduling

    args = inputs(metered=True)
    root = tmp_path / "research"
    with pytest.raises(ExamError, match="request-level budgeting"):
        list(scheduling.run_research(*args, root, admission_probe=admit))
    assert not root.exists()


def test_paid_controller_cannot_return_no_budget_before_candidate(monkeypatch, tmp_path):
    from test_research_scheduling import admit, inputs

    from ukrainian_llm_eval import execution, scheduling

    class NullBudgetController:
        def prepare(self, _manifest, _plan):
            return None

        def bind(self, _root):
            return None

        def for_attempt(self, _route, _config, _attempt_id, _admission_receipt):
            return None

    monkeypatch.setattr(
        execution,
        "run_exam",
        lambda *_args, **_kwargs: pytest.fail("unbudgeted paid candidate was executed"),
    )
    root = tmp_path / "research"
    progress = list(
        scheduling.run_research(
            *inputs(metered=True), root, admission_probe=admit,
            request_budget_controller=NullBudgetController(),
        )
    )
    assert progress == [{"status": "stopped", "reason": "admission_failed"}]
    assert EvidenceStore(root / "evidence").verify_all() == {}


def _provider_bound_values(
    tmp_path, *, attempt_id="attempt-provider-bound", ledger_path=None, inline_charge=True,
    usage_bound=False, broad_policy=False, reservation_id="reserve-provider-bound",
):
    route_sha = "b" * 64
    mechanism = {
        "schema": PROVIDER_BOUND_MECHANISM_SCHEMA,
        "route_sha256": route_sha,
        "provider": "fixture-provider",
        "serializer": SERIALIZER,
        "input_bound": {
            "kind": "provider_context_upper_bound",
            "max_input_tokens_per_request": 100,
            "max_requests_per_segment": 3,
            "includes_hidden_provider_framing": True,
            "evidence_sha256": "4" * 64,
        },
        "output_parameter": {
            "name": "max_tokens",
            "max_tokens_per_request": 100,
            "includes_reasoning": True,
            "includes_tool_calls": True,
            "includes_final_output": True,
            "evidence_sha256": "5" * 64,
        },
        "usage": {
            "input_tokens_path": ["prompt_tokens"],
            "output_tokens_path": ["completion_tokens"],
            "output_includes_reasoning": True,
            "reported_cost_path": ["cost"] if inline_charge else None,
            "reported_cost_kind": "account_charge" if inline_charge else None,
            "reported_cost_scope": "request" if inline_charge else None,
        },
        "cache_billing": CACHE_BILLING,
        "max_tool_result_utf8_bytes": 4096,
        "pricing_evidence_sha256": "6" * 64,
        "backend_identity_evidence_sha256": "7" * 64,
    }
    if usage_bound:
        mechanism["schema"] = USAGE_BOUND_MECHANISM_SCHEMA
        mechanism["usage"] = {
            **mechanism["usage"],
            "reported_cost_path": None,
            "reported_cost_kind": None,
            "reported_cost_scope": None,
        }
        mechanism["usage_upper_bound"] = {
            "mode": "conservative_final_usage_upper_bound",
            "max_billable_requests_per_segment": 3,
            "rounding_scope": "per_request_component",
            "rounding_quantum_micro_usd": 2,
            "maximum_additional_fees_micro_usd": 6,
            "usage_semantics_evidence_sha256": "8" * 64,
            "additional_fees_evidence_sha256": "9" * 64,
            "prices_are_maximum_for_backend": True,
            "usage_is_final": True,
            "automatic_paid_retries": False,
            "usage_scope": "request",
            "input_usage_includes_all_billable_categories": True,
        }
    route = {
        "route_id": "fixture",
        "route_sha256": route_sha,
        "request_budget_mechanism_sha256": digest(mechanism),
        "pricing_evidence_sha256": "6" * 64,
        "billing": {
            "kind": "metered",
            "input_micro_usd_per_million_tokens": 1_000_000,
            "output_micro_usd_per_million_tokens": 1_000_000,
            "max_total_input_tokens": 306 if usage_bound else 300,
            "max_total_output_tokens": 300,
            "max_tool_rounds": 2,
            "tool_round_micro_usd": 3,
        },
    }
    config = {
        "adapter": "chat-http", "provider": "fixture-provider", "max_output_tokens": 100,
        "max_tool_calls": 2,
    }
    policy = {
        "schema": (
            "ukrainian-llm-eval.spending-policy.v2"
            if usage_bound or broad_policy else "ukrainian-llm-eval.spending-policy.v1"
        ),
        "mode": "sequential_shared_cap", "ledger_id": "issue5-public-evaluator",
        "authorized_cap_micro_usd": 1_000,
        "reservation_scope": "whole_segment_before_first_request",
        "settlement": (
            "authoritative_account_charge_or_conservative_final_usage_upper_bound"
            if usage_bound or broad_policy else "authoritative_final_account_charge_only"
        ),
        "cap_stop": "not_executed_budget",
    }
    spec = {
        "schema": USAGE_BOUND_ROUTE_SPEC_SCHEMA if usage_bound else PROVIDER_BOUND_ROUTE_SPEC_SCHEMA,
        "mechanism": mechanism,
    }
    controller = RequestBudgetController(
        {"fixture": spec},
        shared_ledger_path=ledger_path or tmp_path / "shared" / "spending.sqlite3",
    )
    controller.prepare(
        {"routes": [route], "suites": [{"limits": {"max_tool_calls": 2, "max_output_tokens": 100}}],
         "spending_policy": policy},
        {"cells": []},
    )
    root = tmp_path / "run"
    root.mkdir()
    controller.bind(root)
    budget = controller.for_attempt(
        route, config, attempt_id,
        {"credit_available_micro_usd": None, "account_sha256": "c" * 64},
        reservation_id=reservation_id,
        reservation_binding={"candidate_attempt_id": attempt_id},
    )
    return route, config, controller, budget


@pytest.mark.parametrize("broad_policy", [False, True])
def test_provider_bound_mechanism_labels_upper_bound_and_settles_authoritative_charge(tmp_path, broad_policy):
    _route, _config, controller, budget = _provider_bound_values(tmp_path, broad_policy=broad_policy)
    raw, commitment = budget.commit_request(
        {"model": "fixture", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    )
    assert len(raw) < commitment["input_tokens"]
    assert commitment["input_bound_kind"] == "provider_context_upper_bound"
    budget.observe({"prompt_tokens": 12, "completion_tokens": 5, "cost": "0.000010"}, tool_calls=0)
    receipt = budget.finalize("completed")
    assert receipt["result"]["schema"] == "ukrainian-llm-eval.request-budget-receipt.v2"
    assert receipt["result"]["input_tokens_committed"] == 100
    ledger = controller._shared_ledger
    assert ledger.get("reserve-provider-bound")["settled_micro_usd"] == 10
    assert ledger.snapshot()["remaining_new_spend_micro_usd"] == 990


@pytest.mark.parametrize("broad_policy", [False, True])
def test_provider_bound_timeout_keeps_full_shared_reservation(tmp_path, broad_policy):
    _route, _config, _controller, budget = _provider_bound_values(tmp_path, broad_policy=broad_policy)
    budget.commit_request(
        {"model": "fixture", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    )
    receipt = budget.finalize("failed")
    assert receipt["result"]["shared_settlement_sha256"] is None
    ledger = SharedSpendingLedger(
        tmp_path / "shared" / "spending.sqlite3",
        ledger_id="issue5-public-evaluator", cap_micro_usd=1_000,
    )
    assert ledger.get("reserve-provider-bound")["state"] == "unresolved"
    assert ledger.snapshot()["unresolved_new_spend_micro_usd"] == 606


@pytest.mark.parametrize("broad_policy", [False, True])
def test_valid_usage_without_inline_charge_keeps_full_reservation_across_roots(tmp_path, broad_policy):
    _route, _config, _controller, budget = _provider_bound_values(tmp_path, inline_charge=False, broad_policy=broad_policy)
    budget.commit_request(
        {"model": "fixture", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    )
    budget.observe({"prompt_tokens": 12, "completion_tokens": 5}, tool_calls=0)
    receipt = budget.finalize("completed")
    assert receipt["result"]["shared_settlement_sha256"] is None
    restarted = SharedSpendingLedger(
        tmp_path / "shared" / "spending.sqlite3",
        ledger_id="issue5-public-evaluator", cap_micro_usd=1_000,
    )
    assert restarted.get("reserve-provider-bound")["state"] == "unresolved"
    assert restarted.snapshot()["unresolved_new_spend_micro_usd"] == 606


@pytest.mark.parametrize("override", [
    {"schema": []}, {"settlement": {}},
    {"schema": "ukrainian-llm-eval.spending-policy.v2"},
])
def test_authoritative_route_rejects_malformed_or_mismatched_policy_pair(tmp_path, override):
    route, _config, original, _budget = _provider_bound_values(tmp_path)
    replacement = RequestBudgetController({"fixture": {
        "schema": PROVIDER_BOUND_ROUTE_SPEC_SCHEMA, "mechanism": original.route_specs["fixture"]["mechanism"],
    }}, shared_ledger_path=tmp_path / "other.sqlite3")
    with pytest.raises(ExamError, match="settlement policy differ"):
        replacement.prepare({
            "routes": [route], "suites": [{"limits": {"max_tool_calls": 2, "max_output_tokens": 100}}],
            "spending_policy": {**original._spending_policy, **override},
        }, {"cells": []})
    assert not (tmp_path / "other.sqlite3").exists()


def test_authoritative_route_reuses_usage_bound_ledger_without_resetting_cap(tmp_path):
    first_root = tmp_path / "first"
    first_root.mkdir()
    shared = tmp_path / "shared" / "spending.sqlite3"
    _route, _config, first_controller, first = _provider_bound_values(
        first_root, ledger_path=shared, usage_bound=True,
    )
    blocked_root = tmp_path / "blocked"
    blocked_root.mkdir()
    with pytest.raises(SpendingCapExceeded):
        _provider_bound_values(
            blocked_root, ledger_path=shared, broad_policy=True,
            attempt_id="blocked-authoritative", reservation_id="blocked-authoritative",
        )
    payload = {"model": "fixture", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    first.commit_request(payload)
    first.observe({"prompt_tokens": 11, "completion_tokens": 5}, tool_calls=0)
    first_receipt = first.finalize("completed")
    assert first_receipt["result"]["charge_upper_bound_micro_usd"] == 24

    second_root = tmp_path / "second"
    second_root.mkdir()
    _route, _config, second_controller, second = _provider_bound_values(
        second_root, ledger_path=shared, broad_policy=True,
        attempt_id="second-authoritative", reservation_id="second-authoritative",
    )
    assert second_controller._shared_ledger.snapshot()["remaining_new_spend_micro_usd"] == 1_000 - 24 - 606
    second.commit_request(payload)
    second.observe({"prompt_tokens": 12, "completion_tokens": 5, "cost": "0.000010"}, tool_calls=0)
    receipt = second.finalize("completed")
    assert receipt["result"]["schema"] == "ukrainian-llm-eval.request-budget-receipt.v2"
    assert second_controller._shared_ledger.get("second-authoritative")["settled_micro_usd"] == 10
    assert first_controller._shared_ledger.snapshot()["remaining_new_spend_micro_usd"] == 966


def test_configured_authoritative_charge_path_cannot_disappear(tmp_path):
    _route, _config, _controller, budget = _provider_bound_values(tmp_path)
    budget.commit_request(
        {"model": "fixture", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    )
    with pytest.raises(RequestBudgetError, match="provider-reported cost"):
        budget.observe({"prompt_tokens": 12, "completion_tokens": 5}, tool_calls=0)


def test_shared_ledger_cannot_be_scoped_under_execution_root(tmp_path):
    with pytest.raises(ExamError, match="outside the execution root"):
        _provider_bound_values(tmp_path, ledger_path=tmp_path / "run" / "spending.sqlite3")


def test_prepare_checks_metered_plan_reservation_without_equating_existing_credit(tmp_path):
    route, _config, controller, _budget = _provider_bound_values(tmp_path)
    manifest = {
        "routes": [route],
        "suites": [{"limits": {"max_tool_calls": 2, "max_output_tokens": 100}}],
        "spending_policy": controller._spending_policy,
    }
    with pytest.raises(ExamError, match="reservation differs"):
        controller.prepare(
            manifest,
            {"cells": [{"route_id": "fixture", "segments": [{"reserved_micro_usd": 605}]}]},
        )

    existing_credit_route = {**route, "billing": {**route["billing"], "kind": "existing_credit"}}
    controller.prepare(
        {**manifest, "routes": [existing_credit_route]},
        {"cells": [{"route_id": "fixture", "segments": [{"reserved_micro_usd": 0}]}]},
    )


def _renamed_budget_route(route, mechanism, route_id, route_sha256):
    renamed_mechanism = json.loads(json.dumps(mechanism))
    renamed_mechanism["route_sha256"] = route_sha256
    renamed_route = json.loads(json.dumps(route))
    renamed_route["route_id"] = route_id
    renamed_route["route_sha256"] = route_sha256
    renamed_route["request_budget_mechanism_sha256"] = digest(renamed_mechanism)
    return renamed_route, renamed_mechanism


@pytest.mark.parametrize("legacy_kind", ["metered", "existing_credit"])
@pytest.mark.parametrize("usage_bound", [False, True])
def test_sequential_policy_rejects_mixed_legacy_paid_route_for_all_provider_bound_versions(
    tmp_path, legacy_kind, usage_bound,
):
    legacy_root = tmp_path / f"legacy-{legacy_kind}-{usage_bound}"
    legacy_root.mkdir()
    legacy_route, _legacy_config, legacy_mechanism, legacy_controller, _legacy_budget = _values(
        legacy_root,
        kind=legacy_kind,
        credit=1_000_000 if legacy_kind == "existing_credit" else None,
    )
    legacy_route, legacy_mechanism = _renamed_budget_route(
        legacy_route, legacy_mechanism, "legacy", "d" * 64,
    )
    legacy_spec = {
        "schema": ROUTE_SPEC_SCHEMA,
        "mechanism": legacy_mechanism,
        "counter_command": legacy_controller.route_specs["fixture"]["counter_command"],
    }

    provider_root = tmp_path / f"provider-{legacy_kind}-{usage_bound}"
    provider_root.mkdir()
    provider_route, _provider_config, provider_controller, _provider_budget = _provider_bound_values(
        provider_root, usage_bound=usage_bound,
    )
    provider_route, provider_mechanism = _renamed_budget_route(
        provider_route,
        provider_controller.route_specs["fixture"]["mechanism"],
        "provider",
        "e" * 64,
    )
    provider_spec = {
        "schema": USAGE_BOUND_ROUTE_SPEC_SCHEMA if usage_bound else PROVIDER_BOUND_ROUTE_SPEC_SCHEMA,
        "mechanism": provider_mechanism,
    }
    controller = RequestBudgetController(
        {"legacy": legacy_spec, "provider": provider_spec},
        shared_ledger_path=tmp_path / f"mixed-{legacy_kind}-{usage_bound}.sqlite3",
    )

    with pytest.raises(ExamError, match="provider-bound request budgets for every paid route"):
        controller.prepare(
            {
                "routes": [legacy_route, provider_route],
                "suites": [{"limits": {"max_tool_calls": 2, "max_output_tokens": 100}}],
                "spending_policy": provider_controller._spending_policy,
            },
            {"cells": []},
        )


@pytest.mark.parametrize("usage_bound", [False, True])
def test_sequential_policy_allows_legacy_nonincremental_subscription_with_provider_bound_paid_route(
    tmp_path, usage_bound,
):
    subscription_root = tmp_path / f"subscription-{usage_bound}"
    subscription_root.mkdir()
    subscription_route, _subscription_config, subscription_mechanism, subscription_controller, _budget = _values(
        subscription_root, kind="verified_subscription",
    )
    subscription_route, subscription_mechanism = _renamed_budget_route(
        subscription_route, subscription_mechanism, "subscription", "d" * 64,
    )
    subscription_spec = {
        "schema": ROUTE_SPEC_SCHEMA,
        "mechanism": subscription_mechanism,
        "counter_command": subscription_controller.route_specs["fixture"]["counter_command"],
    }

    provider_root = tmp_path / f"provider-subscription-{usage_bound}"
    provider_root.mkdir()
    provider_route, _provider_config, provider_controller, _provider_budget = _provider_bound_values(
        provider_root, usage_bound=usage_bound,
    )
    provider_route, provider_mechanism = _renamed_budget_route(
        provider_route,
        provider_controller.route_specs["fixture"]["mechanism"],
        "provider",
        "e" * 64,
    )
    provider_spec = {
        "schema": USAGE_BOUND_ROUTE_SPEC_SCHEMA if usage_bound else PROVIDER_BOUND_ROUTE_SPEC_SCHEMA,
        "mechanism": provider_mechanism,
    }
    controller = RequestBudgetController(
        {"subscription": subscription_spec, "provider": provider_spec},
        shared_ledger_path=tmp_path / f"mixed-subscription-{usage_bound}.sqlite3",
    )
    controller.prepare(
        {
            "routes": [subscription_route, provider_route],
            "suites": [{"limits": {"max_tool_calls": 2, "max_output_tokens": 100}}],
            "spending_policy": provider_controller._spending_policy,
        },
        {"cells": []},
    )


def test_usage_bound_settles_complete_ordered_final_usage_with_per_component_rounding(tmp_path):
    route, config, controller, budget = _provider_bound_values(tmp_path, usage_bound=True)
    first = {"model": "fixture", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    raw, _commitment = budget.commit_request(first)
    budget.observe({"prompt_tokens": 11, "completion_tokens": 5}, tool_calls=1)
    raw, _commitment = budget.commit_request(first | {"messages": [{"role": "user", "content": raw.hex()}]})
    budget.observe({"prompt_tokens": 13, "completion_tokens": 7}, tool_calls=0)
    receipt = budget.finalize("completed")

    result = receipt["result"]
    assert result["schema"] == "ukrainian-llm-eval.request-budget-receipt.v3"
    assert result["reported_cost_kind"] is None
    assert result["provider_reported_charge_micro_usd"] is None
    assert result["settlement_kind"] == "conservative_final_usage_upper_bound"
    assert [item["round"] for item in result["ordered_final_usage"]] == [1, 2]
    assert result["ordered_final_usage"][0]["input_charge_upper_bound_micro_usd"] == 12
    assert result["ordered_final_usage"][0]["output_charge_upper_bound_micro_usd"] == 6
    assert result["ordered_final_usage"][0]["tool_charge_upper_bound_micro_usd"] == 3
    assert result["charge_upper_bound_micro_usd"] == 49
    assert result["charge_upper_bound_micro_usd"] <= 612
    settled = controller._shared_ledger.get("reserve-provider-bound")
    assert settled["settlement_kind"] == "conservative_final_usage_upper_bound"
    assert settled["settled_micro_usd"] == 49
    assert controller._shared_ledger.snapshot()["remaining_new_spend_micro_usd"] == 951

    evidence = EvidenceStore(tmp_path / "run" / "request-budget-evidence").verify(receipt["attempt_id"])
    candidate = {
        "status": "ok",
        "identity": {"request_budget_receipt_sha256": digest(evidence)},
    }
    from ukrainian_llm_eval.request_budget import verify_request_budget_evidence

    verify_request_budget_evidence(evidence, route, config, "attempt-provider-bound", candidate)


def test_usage_bound_partial_or_lost_final_usage_retains_whole_reservation(tmp_path):
    _route, _config, controller, budget = _provider_bound_values(tmp_path, usage_bound=True)
    payload = {"model": "fixture", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    budget.commit_request(payload)
    budget.observe({"prompt_tokens": 11, "completion_tokens": 5}, tool_calls=1)
    budget.commit_request(payload)
    receipt = budget.finalize("failed")

    assert len(receipt["result"]["ordered_final_usage"]) == 1
    assert receipt["result"]["charge_upper_bound_micro_usd"] is None
    assert receipt["result"]["shared_settlement_sha256"] is None
    reservation = controller._shared_ledger.get("reserve-provider-bound")
    assert reservation["state"] == "unresolved"
    assert controller._shared_ledger.snapshot()["unresolved_new_spend_micro_usd"] == 612

    malformed_root = tmp_path / "malformed"
    malformed_root.mkdir()
    _route, _config, malformed_controller, malformed = _provider_bound_values(
        malformed_root, usage_bound=True
    )
    malformed.commit_request(payload)
    with pytest.raises(RequestBudgetError, match="missing required accounting"):
        malformed.observe({"prompt_tokens": 11}, tool_calls=0)
    malformed_receipt = malformed.finalize("failed")
    assert malformed_receipt["result"]["ordered_final_usage"] == []
    assert malformed_receipt["result"]["charge_upper_bound_micro_usd"] is None
    assert malformed_controller._shared_ledger.snapshot()["unresolved_new_spend_micro_usd"] == 612


def test_usage_bound_requires_frozen_retry_finality_fee_and_worst_case_guarantees(tmp_path):
    route, _config, controller, _budget = _provider_bound_values(tmp_path, usage_bound=True)
    mechanism = controller.route_specs["fixture"]["mechanism"]
    hidden_retry = json.loads(json.dumps(mechanism))
    hidden_retry["usage_upper_bound"]["automatic_paid_retries"] = True
    with pytest.raises(RequestBudgetError, match="retry guarantees"):
        validate_mechanism(hidden_retry)

    too_large = json.loads(json.dumps(mechanism))
    too_large["usage_upper_bound"]["maximum_additional_fees_micro_usd"] += 1
    too_large_route = json.loads(json.dumps(route))
    too_large_route["request_budget_mechanism_sha256"] = digest(too_large)
    with pytest.raises(ExamError, match="exceeds frozen segment reservation"):
        replacement = RequestBudgetController({
            "fixture": {"schema": USAGE_BOUND_ROUTE_SPEC_SCHEMA, "mechanism": too_large}
        })
        replacement.prepare(
            {
                "routes": [too_large_route],
                "suites": [{"limits": {"max_tool_calls": 2, "max_output_tokens": 100}}],
                "spending_policy": {
                    "schema": "ukrainian-llm-eval.spending-policy.v2",
                    "mode": "sequential_shared_cap",
                    "ledger_id": "issue5-public-evaluator",
                    "authorized_cap_micro_usd": 1_000,
                    "reservation_scope": "whole_segment_before_first_request",
                    "settlement": (
                        "authoritative_account_charge_or_conservative_final_usage_upper_bound"
                    ),
                    "cap_stop": "not_executed_budget",
                },
            },
            {"cells": []},
        )

    wrong_policy = RequestBudgetController(
        {"fixture": {"schema": USAGE_BOUND_ROUTE_SPEC_SCHEMA, "mechanism": mechanism}},
        shared_ledger_path=tmp_path / "other-shared" / "spending.sqlite3",
    )
    with pytest.raises(ExamError, match="settlement policy differ"):
        wrong_policy.prepare(
            {
                "routes": [route],
                "suites": [{"limits": {"max_tool_calls": 2, "max_output_tokens": 100}}],
                "spending_policy": {
                    "schema": "ukrainian-llm-eval.spending-policy.v1",
                    "mode": "sequential_shared_cap",
                    "ledger_id": "issue5-public-evaluator",
                    "authorized_cap_micro_usd": 1_000,
                    "reservation_scope": "whole_segment_before_first_request",
                    "settlement": "authoritative_final_account_charge_only",
                    "cap_stop": "not_executed_budget",
                },
            },
            {"cells": []},
        )
