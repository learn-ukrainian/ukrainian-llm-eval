import json
from datetime import UTC, datetime, timedelta

import pytest
from test_admission_command import _command_spec, _fixture

from ukrainian_llm_eval.admission import (
    LIVE_SUBSCRIPTION_RESULT_SCHEMA,
    RESULT_SCHEMA,
    build_admission_request,
    invoke_validated_admission,
    validate_admission_result,
)
from ukrainian_llm_eval.admission_command import command_identity_sha256
from ukrainian_llm_eval.core import ExamError, digest
from ukrainian_llm_eval.evidence import EvidenceStore


def inputs(kind="metered"):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    zero = kind != "metered"
    rate = 0 if kind == "verified_subscription" else 1000
    route = {"route_sha256": "a" * 64, "conditions": ["closed-book", "sources"],
             "billing": {"kind": kind, "input_micro_usd_per_million_tokens": rate,
                         "output_micro_usd_per_million_tokens": rate, "tool_round_micro_usd": 0,
                         "max_total_input_tokens": 10000, "max_total_output_tokens": 300}}
    config = {"model": "fixture", "effort": None, "max_output_tokens": 100,
              "max_tool_calls": 2, "timeout_seconds": 10}
    auth = {"schema": "ukrainian-llm-eval.operator-authorization.v1", "route_sha256": route["route_sha256"],
            "allow_paid": not zero, "max_new_spend_micro_usd": 10_000_000 if not zero else 0}
    route["operator_authorization_sha256"] = digest(auth)
    states = {
        "pricing": {"route_sha256": route["route_sha256"], "currency": "USD",
                    "input_micro_usd_per_million_tokens": rate, "output_micro_usd_per_million_tokens": rate,
                    "tool_round_micro_usd": 0},
        "entitlement": {"route_sha256": route["route_sha256"], "account_sha256": "b" * 64,
                        "billing_kind": kind, "zero_incremental": zero, "valid_until": (now + timedelta(hours=1)).isoformat()},
        "capability": {"route_sha256": route["route_sha256"], "model": "fixture", "effort": None,
                       "context_input_tokens": 10000, "max_output_tokens": 100, "max_tool_calls": 2,
                       "timeout_seconds": 10, "tool_policy_sha256": "c" * 64},
    }
    request = build_admission_request(route, config, "sources", input_utf8_bytes=1000,
                                      tool_policy_sha256="c" * 64, composite_sha256="d" * 64, now=now)
    result = {"schema": RESULT_SCHEMA, "nonce": request["nonce"], "request_sha256": request["request_sha256"],
              "observed_at": now.isoformat()}
    observations = {
        "pricing": {"conservative_segment_cost_micro_usd": 11 if rate else 0,
                    "incremental_segment_cost_micro_usd": 0 if zero else 11},
        "entitlement": {"eligible": True, "credit_available_micro_usd": 50 if kind == "existing_credit" else None},
        "capability": {"healthy": True, "input_fits": True, "required_input_tokens": 500},
    }
    for name, state in states.items():
        route[name + "_evidence_sha256"] = digest(state)
        result[name] = {"state": state, "state_sha256": digest(state), "observed": observations[name]}
    kwargs = {"reserved_micro_usd": 0 if zero else 11, "remaining_ceiling_micro_usd": 10_000_000,
              "operator_authorization": auth, "max_age_seconds": 30, "now": now + timedelta(seconds=1)}
    return result, request, route, config, kwargs


@pytest.mark.parametrize("kind", ["metered", "verified_subscription", "existing_credit"])
def test_admits_fresh_explicit_claims_and_separate_authorization(kind):
    result, request, route, config, kwargs = inputs(kind)
    receipt = validate_admission_result(result, request, route, config, **kwargs)
    assert receipt["incremental_segment_cost_micro_usd"] == (11 if kind == "metered" else 0)
    assert receipt["result_sha256"] == digest(result)
    assert "reserved_micro_usd" not in repr(request) and "responses" not in repr(request)


@pytest.mark.parametrize("kind", ["verified_subscription", "existing_credit"])
def test_exact_route_record_authorizes_use_without_authorizing_new_spend(kind):
    result, request, route, config, kwargs = inputs(kind)
    authorization = kwargs["operator_authorization"]
    assert authorization["allow_paid"] is False
    assert authorization["max_new_spend_micro_usd"] == 0

    receipt = validate_admission_result(result, request, route, config, **kwargs)
    assert receipt["operator_authorization_sha256"] == route["operator_authorization_sha256"]
    assert receipt["incremental_segment_cost_micro_usd"] == 0


@pytest.mark.parametrize("change", ["nonce", "stale", "future", "extra", "unhealthy", "unknown_fit",
                                    "underquoted", "overquoted", "unauthorized", "expired", "changed_state"])
def test_admission_rejects_unknown_stale_or_unsafe_claims(change):
    result, request, route, config, kwargs = inputs()
    if change == "nonce": result["nonce"] = "not-the-request"
    elif change == "stale": kwargs["now"] += timedelta(seconds=40)
    elif change == "future": result["observed_at"] = (kwargs["now"] + timedelta(seconds=1)).isoformat()
    elif change == "extra": result["pricing"]["observed"]["pass"] = True
    elif change == "unhealthy": result["capability"]["observed"]["healthy"] = False
    elif change == "unknown_fit": result["capability"]["observed"]["input_fits"] = None
    elif change == "underquoted": result["pricing"]["observed"]["conservative_segment_cost_micro_usd"] = 10
    elif change == "overquoted":
        result["pricing"]["observed"] = {"conservative_segment_cost_micro_usd": 12, "incremental_segment_cost_micro_usd": 12}
    elif change == "unauthorized":
        kwargs["operator_authorization"]["allow_paid"] = False
        route["operator_authorization_sha256"] = digest(kwargs["operator_authorization"])
    elif change == "expired":
        state = result["entitlement"]["state"]
        state["valid_until"] = request["requested_at"]
        result["entitlement"]["state_sha256"] = route["entitlement_evidence_sha256"] = digest(state)
    else: result["capability"]["state"]["model"] = "other"
    with pytest.raises(ExamError):
        validate_admission_result(result, request, route, config, **kwargs)


def test_credit_balance_can_change_without_state_drift_but_must_cover_charge():
    result, request, route, config, kwargs = inputs("existing_credit")
    old_hash = result["entitlement"]["state_sha256"]
    result["entitlement"]["observed"]["credit_available_micro_usd"] = 11
    validate_admission_result(result, request, route, config, **kwargs)
    assert result["entitlement"]["state_sha256"] == old_hash
    result["entitlement"]["observed"]["credit_available_micro_usd"] = 10
    with pytest.raises(ExamError, match="credit insufficient"):
        validate_admission_result(result, request, route, config, **kwargs)


def live_subscription_inputs():
    result, request, route, config, kwargs = inputs("verified_subscription")
    result["schema"] = LIVE_SUBSCRIPTION_RESULT_SCHEMA
    state = result["entitlement"]["state"]
    state.update(valid_until=None, verification="live_subscription")
    result["entitlement"]["state_sha256"] = route["entitlement_evidence_sha256"] = digest(state)
    result["entitlement"]["observed"].update(
        subscription_status="active", paid_fallback_enabled=False, status_observed_at=result["observed_at"]
    )
    return result, request, route, config, kwargs


@pytest.mark.parametrize("known_expiry", [False, True])
def test_live_subscription_expiry_is_explicit_without_inventing_a_future_date(known_expiry):
    result, request, route, config, kwargs = live_subscription_inputs()
    if known_expiry:
        state = result["entitlement"]["state"]
        state["valid_until"] = (kwargs["now"] + timedelta(hours=1)).isoformat()
        result["entitlement"]["state_sha256"] = route["entitlement_evidence_sha256"] = digest(state)
    receipt = validate_admission_result(result, request, route, config, **kwargs)
    assert receipt["result_sha256"] == digest(result)
    assert receipt["incremental_segment_cost_micro_usd"] == 0
    assert (result["entitlement"]["state"]["valid_until"] is None) == (not known_expiry)


@pytest.mark.parametrize("change", [
    "inactive", "unknown", "ineligible", "fallback", "fallback_unknown", "fallback_zero",
    "old_status", "future_status", "stale_result", "expired", "wrong_verification", "account_drift",
    "metered", "credit", "legacy_schema", "missing_status", "unauthorized", "nonce",
])
def test_live_subscription_never_turns_unknown_or_stale_entitlement_into_permission(change):
    result, request, route, config, kwargs = live_subscription_inputs()
    state = result["entitlement"]["state"]
    live = result["entitlement"]["observed"]
    if change == "inactive": live["subscription_status"] = "inactive"
    elif change == "unknown": live["subscription_status"] = "unknown"
    elif change == "ineligible": live["eligible"] = False
    elif change == "fallback": live["paid_fallback_enabled"] = True
    elif change == "fallback_unknown": live["paid_fallback_enabled"] = None
    elif change == "fallback_zero": live["paid_fallback_enabled"] = 0
    elif change == "old_status":
        live["status_observed_at"] = (kwargs["now"] - timedelta(seconds=2)).isoformat()
    elif change == "future_status": live["status_observed_at"] = kwargs["now"].isoformat()
    elif change == "stale_result": kwargs["now"] += timedelta(seconds=40)
    elif change in {"expired", "wrong_verification", "metered", "credit"}:
        if change == "expired": state["valid_until"] = request["requested_at"]
        elif change == "wrong_verification": state["verification"] = "quota_reset"
        else:
            state["billing_kind"] = route["billing"]["kind"] = "metered" if change == "metered" else "existing_credit"
        result["entitlement"]["state_sha256"] = route["entitlement_evidence_sha256"] = digest(state)
    elif change == "account_drift": state["account_sha256"] = "e" * 64
    elif change == "legacy_schema": result["schema"] = RESULT_SCHEMA
    elif change == "missing_status": live.pop("status_observed_at")
    elif change == "unauthorized": kwargs["operator_authorization"]["route_sha256"] = "e" * 64
    elif change == "nonce": result["nonce"] = "f" * 32
    with pytest.raises(ExamError):
        validate_admission_result(result, request, route, config, **kwargs)


def test_legacy_subscription_still_requires_a_provider_expiry():
    result, request, route, config, kwargs = inputs("verified_subscription")
    state = result["entitlement"]["state"]
    state["valid_until"] = None
    result["entitlement"]["state_sha256"] = route["entitlement_evidence_sha256"] = digest(state)
    with pytest.raises(ExamError, match="invalid admission timestamp"):
        validate_admission_result(result, request, route, config, **kwargs)


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("live_subscription", [True, False])
def test_real_probe_pipeline_preserves_claims_or_only_safe_rejection_hashes(tmp_path, valid, live_subscription):
    result, _, route, config, kwargs = live_subscription_inputs() if live_subscription else inputs()
    now = datetime.now(UTC)
    state = result["entitlement"]["state"]
    state["valid_until"] = None if live_subscription else (now + timedelta(hours=1)).isoformat()
    route["entitlement_evidence_sha256"] = result["entitlement"]["state_sha256"] = digest(state)
    request = build_admission_request(route, config, "sources", input_utf8_bytes=1000,
                                      tool_policy_sha256="c" * 64, composite_sha256="d" * 64, now=now)
    source = "import json, sys\nfrom datetime import datetime, timezone\nrequest=json.load(sys.stdin)\n"
    if valid:
        source += "result=json.loads(" + repr(json.dumps(result)) + ")\n"
        source += "result.update(nonce=request['nonce'], request_sha256=request['request_sha256'], observed_at=datetime.now(timezone.utc).isoformat())\nprint(json.dumps(result))\n"
        if live_subscription:
            source = source.replace("print(json.dumps(result))", "result['entitlement']['observed']['status_observed_at']=result['observed_at']\nprint(json.dumps(result))")
    else:
        source += "print('ADMISSION_PRIVATE_SENTINEL')\n"
    script, lock = _fixture(tmp_path, source)
    spec = _command_spec(script, lock, stdout_max_bytes=8192)
    route["admission_command_sha256"] = command_identity_sha256(spec)
    kwargs.pop("now")
    root = tmp_path / "evidence"
    if valid:
        receipt, evidence = invoke_validated_admission(spec, request, route, config, root, **kwargs)
        assert evidence["terminal_status"] == "completed"
        assert receipt["request_sha256"] == request["request_sha256"]
        assert evidence["result"] == receipt
    else:
        with pytest.raises(ExamError, match="claims rejected"):
            invoke_validated_admission(spec, request, route, config, root, **kwargs)
        stored = next(iter(EvidenceStore(root).verify_all().values()))
        assert stored["terminal_status"] == "failed"
        assert stored["result"]["reason"] == "invalid_probe_claims"
        assert all(b"ADMISSION_PRIVATE_SENTINEL" not in p.read_bytes() for p in root.rglob('*') if p.is_file())
