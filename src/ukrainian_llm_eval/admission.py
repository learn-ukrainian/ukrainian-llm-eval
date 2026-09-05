"""Strict, nonce-bound admission claims for trusted controller integrations.

State hashes freeze declared terms. Fresh observations are evaluated separately;
neither an entitlement nor a matching hash is itself authority to spend money.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from datetime import UTC, datetime

from .core import ExamError, _duplicate_rejecting_pairs, _reject_json_constant, digest

REQUEST_SCHEMA = "ukrainian-llm-eval.admission-request.v1"
RESULT_SCHEMA = "ukrainian-llm-eval.admission-result.v1"


def _exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ExamError("invalid admission " + label)
    return value


def _integer(value):
    if type(value) is not int or value < 0:
        raise ExamError("invalid admission non-negative integer")
    return value


def _sha(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ExamError("invalid admission digest")
    return value


def _time(value):
    if not isinstance(value, str):
        raise ExamError("invalid admission timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExamError("invalid admission timestamp") from exc
    if parsed.tzinfo is None:
        raise ExamError("admission timestamp requires timezone")
    return parsed.astimezone(UTC)


def build_admission_request(route, config, condition, *, input_utf8_bytes, tool_policy_sha256,
                            composite_sha256, now=None):
    """Pass only safe bounds and route identity, never question text or reserves."""
    if condition not in route["conditions"]:
        raise ExamError("admission condition not frozen")
    observed = now or datetime.now(UTC)
    if observed.tzinfo is None:
        raise ExamError("admission clock requires timezone")
    billing = route["billing"]
    body = {"schema": REQUEST_SCHEMA, "nonce": uuid.uuid4().hex,
            "requested_at": observed.astimezone(UTC).isoformat(),
            "route_sha256": _sha(route["route_sha256"]), "model": config["model"], "effort": config["effort"],
            "condition": condition, "composite_sha256": _sha(composite_sha256),
            "requirements": {"input_utf8_bytes": _integer(input_utf8_bytes),
                             "max_total_input_tokens": _integer(billing["max_total_input_tokens"]),
                             "max_total_output_tokens": _integer(billing["max_total_output_tokens"]),
                             "max_output_tokens": _integer(config["max_output_tokens"]),
                             "max_tool_calls": _integer(config["max_tool_calls"]) if condition == "sources" else 0,
                             "timeout_seconds": _integer(config["timeout_seconds"]),
                             "tool_policy_sha256": _sha(tool_policy_sha256)}}
    return {**body, "request_sha256": digest(body)}


def _record(value, expected_hash, fields, observations, label):
    _exact(value, {"state", "state_sha256", "observed"}, label)
    state = _exact(value["state"], fields, label + " state")
    live = _exact(value["observed"], observations, label + " observations")
    if _sha(value["state_sha256"]) != digest(state) or value["state_sha256"] != expected_hash:
        raise ExamError("admission stable state drift")
    return state, live


def validate_admission_result(result, request, route, config, *, reserved_micro_usd,
                              remaining_ceiling_micro_usd, operator_authorization,
                              max_age_seconds, now=None):
    """Validate fresh claims, cost arithmetic and route-bound new-spend authorization."""
    clock = now or datetime.now(UTC)
    _integer(max_age_seconds)
    if max_age_seconds == 0 or clock.tzinfo is None:
        raise ExamError("invalid admission freshness limit")
    _exact(request, {"schema", "nonce", "requested_at", "route_sha256", "model", "effort", "condition",
                     "composite_sha256", "requirements", "request_sha256"}, "request")
    if request["schema"] != REQUEST_SCHEMA or not isinstance(request["nonce"], str) or re.fullmatch(r"[0-9a-f]{32}", request["nonce"]) is None:
        raise ExamError("invalid admission request identity")
    _exact(request["requirements"], {"input_utf8_bytes", "max_total_input_tokens", "max_total_output_tokens",
                                     "max_output_tokens", "max_tool_calls", "timeout_seconds", "tool_policy_sha256"}, "requirements")
    if request["route_sha256"] != route["route_sha256"] or request["model"] != config["model"] or request["effort"] != config["effort"]:
        raise ExamError("admission request route/config mismatch")
    if request["condition"] not in route["conditions"]:
        raise ExamError("admission request condition not frozen")
    if request.get("request_sha256") != digest({k: v for k, v in request.items() if k != "request_sha256"}):
        raise ExamError("admission request hash mismatch")
    _exact(result, {"schema", "nonce", "request_sha256", "observed_at", "pricing", "entitlement", "capability"}, "result")
    if result["schema"] != RESULT_SCHEMA or result["nonce"] != request["nonce"] or result["request_sha256"] != request["request_sha256"]:
        raise ExamError("admission request/nonce binding mismatch")
    requested, observed = _time(request["requested_at"]), _time(result["observed_at"])
    clock = clock.astimezone(UTC)
    if not requested <= observed <= clock or (clock - requested).total_seconds() > max_age_seconds:
        raise ExamError("admission observation is stale or future dated")
    pricing, quote = _record(result["pricing"], route["pricing_evidence_sha256"],
                             {"route_sha256", "currency", "input_micro_usd_per_million_tokens",
                              "output_micro_usd_per_million_tokens", "tool_round_micro_usd"},
                             {"conservative_segment_cost_micro_usd", "incremental_segment_cost_micro_usd"}, "pricing")
    entitlement, account = _record(result["entitlement"], route["entitlement_evidence_sha256"],
                                   {"route_sha256", "account_sha256", "billing_kind", "zero_incremental", "valid_until"},
                                   {"eligible", "credit_available_micro_usd"}, "entitlement")
    capability, health = _record(result["capability"], route["capability_evidence_sha256"],
                                 {"route_sha256", "model", "effort", "context_input_tokens", "max_output_tokens",
                                  "max_tool_calls", "timeout_seconds", "tool_policy_sha256"},
                                 {"healthy", "input_fits", "required_input_tokens"}, "capability")
    if any(state["route_sha256"] != route["route_sha256"] for state in (pricing, entitlement, capability)):
        raise ExamError("admission route identity drift")
    if pricing["currency"] != "USD":
        raise ExamError("admission currency mismatch")
    billing = route["billing"]
    for field in ("input_micro_usd_per_million_tokens", "output_micro_usd_per_million_tokens", "tool_round_micro_usd"):
        if _integer(pricing[field]) != billing[field]:
            raise ExamError("admission pricing drift")
    expected_charge = sum((pricing[field] * request["requirements"][tokens] + 999_999) // 1_000_000
                          for field, tokens in (("input_micro_usd_per_million_tokens", "max_total_input_tokens"),
                                                ("output_micro_usd_per_million_tokens", "max_total_output_tokens")))
    expected_charge += pricing["tool_round_micro_usd"] * request["requirements"]["max_tool_calls"]
    charge = _integer(quote["conservative_segment_cost_micro_usd"])
    incremental = _integer(quote["incremental_segment_cost_micro_usd"])
    if charge < expected_charge:
        raise ExamError("admission cost bound is below frozen arithmetic")
    _sha(entitlement["account_sha256"])
    if entitlement["billing_kind"] != billing["kind"] or type(entitlement["zero_incremental"]) is not bool:
        raise ExamError("admission billing state drift")
    if _time(entitlement["valid_until"]) <= clock or account["eligible"] is not True:
        raise ExamError("admission entitlement expired or ineligible")
    if billing["kind"] == "metered":
        if entitlement["zero_incremental"] or incremental != charge or account["credit_available_micro_usd"] is not None:
            raise ExamError("admission metered charge mismatch")
    else:
        if not entitlement["zero_incremental"] or incremental != 0:
            raise ExamError("admission is not zero incremental spend")
        if billing["kind"] == "existing_credit":
            if _integer(account["credit_available_micro_usd"]) < charge:
                raise ExamError("admission existing credit insufficient")
        elif account["credit_available_micro_usd"] is not None:
            raise ExamError("admission subscription must not imply credit balance")
    if incremental > _integer(reserved_micro_usd) or incremental > _integer(remaining_ceiling_micro_usd):
        raise ExamError("admission exceeds frozen reservation or ceiling")
    authorization = _exact(operator_authorization, {"schema", "route_sha256", "allow_paid", "max_new_spend_micro_usd"}, "operator authorization")
    if authorization["schema"] != "ukrainian-llm-eval.operator-authorization.v1" or authorization["route_sha256"] != route["route_sha256"]:
        raise ExamError("admission operator authorization route mismatch")
    if digest(authorization) != route["operator_authorization_sha256"]:
        raise ExamError("admission operator authorization drift")
    if type(authorization["allow_paid"]) is not bool:
        raise ExamError("invalid operator authorization")
    # Supplying the exact hash-bound record authorizes this route. These two
    # fields separately govern incremental new-money charges; zero-incremental
    # subscription and existing-credit routes therefore use false/zero.
    max_new_spend = _integer(authorization["max_new_spend_micro_usd"])
    if incremental and not authorization["allow_paid"]:
        raise ExamError("admission new spend not authorized")
    if incremental > max_new_spend:
        raise ExamError("admission exceeds operator authorization")
    requirements = request["requirements"]
    if capability["model"] != config["model"] or capability["effort"] != config["effort"]:
        raise ExamError("admission model or effort identity drift")
    if capability["tool_policy_sha256"] != requirements["tool_policy_sha256"]:
        raise ExamError("admission tool policy drift")
    if health["healthy"] is not True or health["input_fits"] is not True:
        raise ExamError("admission route unhealthy or input does not fit")
    required_input = _integer(health["required_input_tokens"])
    if required_input == 0 and requirements["input_utf8_bytes"]:
        raise ExamError("admission input token claim cannot be zero for a nonempty request")
    if required_input > _integer(capability["context_input_tokens"]) or required_input > requirements["max_total_input_tokens"]:
        raise ExamError("admission input bound exceeds context or reserved input")
    for field in ("max_output_tokens", "max_tool_calls", "timeout_seconds"):
        if _integer(capability[field]) < requirements[field]:
            raise ExamError("admission route cannot satisfy segment limits")
    return {"schema": "ukrainian-llm-eval.admission-receipt.v1", "request_sha256": request["request_sha256"],
            "result_sha256": digest(result), "composite_sha256": request["composite_sha256"],
            "pricing_state_sha256": result["pricing"]["state_sha256"],
            "entitlement_state_sha256": result["entitlement"]["state_sha256"],
            "capability_state_sha256": result["capability"]["state_sha256"],
            "operator_authorization_sha256": digest(authorization), "incremental_segment_cost_micro_usd": incremental,
            "credit_available_micro_usd": account["credit_available_micro_usd"],
            "account_sha256": entitlement["account_sha256"]}


def invoke_validated_admission(spec, request, route, config, evidence_dir, *, reserved_micro_usd,
                                remaining_ceiling_micro_usd, operator_authorization, max_age_seconds,
                                execution_binding=None):
    """Retain one trusted probe attempt without retaining rejected raw streams."""
    from .admission_command import command_identity_sha256, invoke_admission
    from .evidence import EvidenceStore

    identity = command_identity_sha256(spec)
    if identity != route["admission_command_sha256"]:
        raise ExamError("admission command differs from frozen identity")
    attempt = EvidenceStore(evidence_dir).start({"denominator": 0, "request_sha256": request["request_sha256"],
                                                "route_sha256": route["route_sha256"], "command_sha256": identity,
                                                "composite_sha256": request["composite_sha256"],
                                                "execution_binding": execution_binding})
    attempt.append("admission_request", request)
    process = invoke_admission(spec, request)
    attempt.append("admission_process", {key: value for key, value in process.items() if key != "stdout"})
    if process["status"] != "success" or process.get("command_identity_sha256") != identity:
        failed = {"schema": "ukrainian-llm-eval.admission-failure.v1", "status": "failed",
                  "reason": "probe_process_failed", "request_sha256": request["request_sha256"]}
        attempt.finalize(failed, status="failed")
        raise ExamError("admission command failed")
    try:
        result = json.loads(process["stdout"].decode("utf-8"), object_pairs_hook=_duplicate_rejecting_pairs,
                            parse_constant=_reject_json_constant)
        receipt = validate_admission_result(result, request, route, config,
                                             reserved_micro_usd=reserved_micro_usd,
                                             remaining_ceiling_micro_usd=remaining_ceiling_micro_usd,
                                             operator_authorization=operator_authorization,
                                             max_age_seconds=max_age_seconds)
    except (ValueError, UnicodeError) as exc:
        failed = {"schema": "ukrainian-llm-eval.admission-failure.v1", "status": "failed",
                  "reason": "invalid_probe_claims", "error_class": type(exc).__name__,
                  "request_sha256": request["request_sha256"]}
        attempt.finalize(failed, status="failed")
        raise ExamError("admission command claims rejected") from exc
    attempt.append("admission_result", result)
    evidence = attempt.finalize(receipt)
    return receipt, evidence


def admission_composite_sha256(manifest, route, segment_plan):
    return digest({"protocol_sha256": manifest["protocol_sha256"],
                   "controller_tool_policy_sha256": manifest["tool_policy_sha256"],
                   "command_declared_identity_sha256": route["admission_command_sha256"],
                   "operator_authorization_sha256": route["operator_authorization_sha256"],
                   "request_budget_mechanism_sha256": route["request_budget_mechanism_sha256"],
                   "route_config_sha256": route["config_sha256"],
                   "segment_plan_sha256": segment_plan["segment_plan_sha256"]})


def verify_admission_evidence(evidence, request_binding, route, composite_sha256, reserved_micro_usd):
    """Verify the saved successful probe before linking or resuming a candidate."""
    if not evidence.get("complete") or evidence.get("terminal_status") != "completed":
        raise ExamError("admission evidence is not a completed approval")
    metadata = evidence["metadata"]
    if metadata.get("execution_binding") != request_binding or metadata.get("route_sha256") != route["route_sha256"]:
        raise ExamError("admission evidence execution binding mismatch")
    if metadata.get("command_sha256") != route["admission_command_sha256"] or metadata.get("composite_sha256") != composite_sha256:
        raise ExamError("admission evidence implementation drift")
    receipt = evidence["result"]
    fields = {"schema", "request_sha256", "result_sha256", "composite_sha256", "pricing_state_sha256",
              "entitlement_state_sha256", "capability_state_sha256", "operator_authorization_sha256",
              "incremental_segment_cost_micro_usd", "credit_available_micro_usd", "account_sha256"}
    _exact(receipt, fields, "evidence receipt")
    if receipt["schema"] != "ukrainian-llm-eval.admission-receipt.v1" or receipt["request_sha256"] != metadata.get("request_sha256"):
        raise ExamError("admission evidence request mismatch")
    for field in fields - {"schema", "incremental_segment_cost_micro_usd", "credit_available_micro_usd"}:
        _sha(receipt[field])
    for kind in ("pricing", "entitlement", "capability"):
        if receipt[kind + "_state_sha256"] != route[kind + "_evidence_sha256"]:
            raise ExamError("admission evidence state mismatch")
    if receipt["composite_sha256"] != composite_sha256 or receipt["operator_authorization_sha256"] != route["operator_authorization_sha256"]:
        raise ExamError("admission evidence authorization mismatch")
    if _integer(receipt["incremental_segment_cost_micro_usd"]) > _integer(reserved_micro_usd):
        raise ExamError("admission evidence exceeds reservation")
    credit = receipt["credit_available_micro_usd"]
    if route["billing"]["kind"] == "existing_credit":
        _integer(credit)
    elif credit is not None:
        raise ExamError("admission evidence has an unexpected credit balance")


class CommandAdmissionController:
    """Only explicitly supplied trusted specs; never discover commands in data."""

    def __init__(self, specs, authorizations):
        import copy

        self.specs = copy.deepcopy(specs)
        self.authorizations = copy.deepcopy(authorizations)

    def prepare(self, manifest, plan):
        from .admission_command import command_identity_sha256

        routes = {route["route_id"]: route for route in manifest["routes"]}
        if set(self.specs) != set(routes) or set(self.authorizations) != set(routes):
            raise ExamError("admission integrations do not cover frozen routes")
        for route_id, route in routes.items():
            if command_identity_sha256(self.specs[route_id]) != route["admission_command_sha256"]:
                raise ExamError("admission command drift")
            auth = self.authorizations[route_id]
            _exact(auth, {"schema", "route_sha256", "allow_paid", "max_new_spend_micro_usd"}, "operator authorization")
            if digest(auth) != route["operator_authorization_sha256"] or auth["route_sha256"] != route["route_sha256"]:
                raise ExamError("operator authorization drift")
            if auth["schema"] != "ukrainian-llm-eval.operator-authorization.v1" or type(auth["allow_paid"]) is not bool:
                raise ExamError("invalid operator authorization")
            total = sum(segment["reserved_micro_usd"] for cell in plan["cells"] if cell["route_id"] == route_id
                        for segment in cell["segments"])
            largest = max(
                (segment["reserved_micro_usd"] for cell in plan["cells"] if cell["route_id"] == route_id
                 for segment in cell["segments"]),
                default=0,
            )
            max_new_spend = _integer(auth["max_new_spend_micro_usd"])
            if total and not auth["allow_paid"]:
                raise ExamError("frozen route schedule has unauthorized new spend")
            sequential = plan.get("spending_policy", {}).get("mode") == "sequential_shared_cap"
            authorized_amount = largest if sequential else total
            if authorized_amount > max_new_spend:
                raise ExamError("frozen route schedule exceeds operator authorization")

    def __call__(self, route, config, condition, *, request, execution_binding, reserved_micro_usd,
                 remaining_ceiling_micro_usd, evidence_dir):
        if condition != request["condition"]:
            raise ExamError("admission condition mismatch")
        spec = self.specs[route["route_id"]]
        return invoke_validated_admission(spec, request, route, config, evidence_dir,
                                          reserved_micro_usd=reserved_micro_usd,
                                          remaining_ceiling_micro_usd=remaining_ceiling_micro_usd,
                                          operator_authorization=self.authorizations[route["route_id"]],
                                          max_age_seconds=math.ceil(spec["timeout_seconds"]) + 1,
                                          execution_binding=execution_binding)
