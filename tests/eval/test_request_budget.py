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
    RequestBudgetController,
    RequestBudgetError,
    validate_mechanism,
)
from ukrainian_llm_eval.spending_ledger import SharedSpendingLedger


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
    tmp_path, *, attempt_id="attempt-provider-bound", ledger_path=None, inline_charge=True
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
    route = {
        "route_id": "fixture",
        "route_sha256": route_sha,
        "request_budget_mechanism_sha256": digest(mechanism),
        "pricing_evidence_sha256": "6" * 64,
        "billing": {
            "kind": "metered",
            "input_micro_usd_per_million_tokens": 1_000_000,
            "output_micro_usd_per_million_tokens": 1_000_000,
            "max_total_input_tokens": 300,
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
        "mode": "sequential_shared_cap", "ledger_id": "issue5-public-evaluator",
        "authorized_cap_micro_usd": 1_000,
    }
    spec = {"schema": PROVIDER_BOUND_ROUTE_SPEC_SCHEMA, "mechanism": mechanism}
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
        reservation_id="reserve-provider-bound",
        reservation_binding={"candidate_attempt_id": attempt_id},
    )
    return route, config, controller, budget


def test_provider_bound_mechanism_labels_upper_bound_and_settles_authoritative_charge(tmp_path):
    _route, _config, controller, budget = _provider_bound_values(tmp_path)
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


def test_provider_bound_timeout_keeps_full_shared_reservation(tmp_path):
    _route, _config, _controller, budget = _provider_bound_values(tmp_path)
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


def test_valid_usage_without_inline_charge_keeps_full_reservation_across_roots(tmp_path):
    _route, _config, _controller, budget = _provider_bound_values(tmp_path, inline_charge=False)
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
