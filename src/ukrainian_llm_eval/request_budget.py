"""Provider-specific request accounting before a paid candidate call.

Version 1 uses a trusted exact counter. Version 2 commits an explicitly labeled
provider-documented context upper bound. Both return the canonical request bytes
that the HTTP adapter transmits.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .admission_command import command_identity_sha256, invoke_admission, validate_command_spec
from .core import ExamError, _duplicate_rejecting_pairs, _reject_json_constant, canonical, digest
from .evidence import EvidenceStore

MECHANISM_SCHEMA = "ukrainian-llm-eval.request-budget-mechanism.v1"
ROUTE_SPEC_SCHEMA = "ukrainian-llm-eval.request-budget-route.v1"
PROVIDER_BOUND_MECHANISM_SCHEMA = "ukrainian-llm-eval.request-budget-mechanism.v2"
PROVIDER_BOUND_ROUTE_SPEC_SCHEMA = "ukrainian-llm-eval.request-budget-route.v2"
USAGE_BOUND_MECHANISM_SCHEMA = "ukrainian-llm-eval.request-budget-mechanism.v3"
USAGE_BOUND_ROUTE_SPEC_SCHEMA = "ukrainian-llm-eval.request-budget-route.v3"
COUNTER_RESULT_SCHEMA = "ukrainian-llm-eval.request-token-count.v1"
SERIALIZER = "canonical-json-utf8-newline-v1"
INPUT_SEMANTICS = "provider-native-full-request-v1"
CACHE_BILLING = "highest-applicable-input-rate"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MECHANISM_FIELDS = {
    "schema",
    "route_sha256",
    "provider",
    "serializer",
    "counter_command_sha256",
    "counter_semantics_sha256",
    "input_semantics",
    "output_parameter",
    "usage",
    "cache_billing",
    "max_tool_result_utf8_bytes",
}
_PROVIDER_BOUND_FIELDS = {
    "schema", "route_sha256", "provider", "serializer", "input_bound", "output_parameter",
    "usage", "cache_billing", "max_tool_result_utf8_bytes", "pricing_evidence_sha256",
    "backend_identity_evidence_sha256",
}
_USAGE_BOUND_FIELDS = _PROVIDER_BOUND_FIELDS | {"usage_upper_bound"}


class RequestBudgetError(ValueError):
    """The paid request could not be bounded before or after transmission."""


def _fail(message: str) -> None:
    raise RequestBudgetError(message)


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _fail(f"invalid {label} digest")
    return value


def _positive(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        _fail(f"invalid {label}")
    return value


def _positive_or_zero(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        _fail(f"invalid {label}")
    return value


def _path(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or _PATH_RE.fullmatch(item) is None for item in value)
    ):
        _fail(f"invalid {label} path")
    return list(value)


def validate_mechanism(value: Any) -> dict[str, Any]:
    """Validate an attested provider mechanism without claiming its accuracy."""

    if isinstance(value, Mapping) and value.get("schema") == PROVIDER_BOUND_MECHANISM_SCHEMA:
        return _validate_provider_bound_mechanism(value)
    if isinstance(value, Mapping) and value.get("schema") == USAGE_BOUND_MECHANISM_SCHEMA:
        return _validate_usage_bound_mechanism(value)
    if not isinstance(value, Mapping) or set(value) != _MECHANISM_FIELDS:
        _fail("invalid request-budget mechanism fields")
    if value["schema"] != MECHANISM_SCHEMA:
        _fail("unsupported request-budget mechanism")
    provider = value["provider"]
    if not isinstance(provider, str) or not provider.strip():
        _fail("invalid request-budget provider")
    if value["serializer"] != SERIALIZER or value["input_semantics"] != INPUT_SEMANTICS:
        _fail("unknown request token-counting semantics")
    if value["cache_billing"] != CACHE_BILLING:
        _fail("unknown cache billing semantics")
    output = value["output_parameter"]
    output_fields = {"name", "includes_reasoning", "includes_tool_calls", "includes_final_output"}
    if not isinstance(output, Mapping) or set(output) != output_fields:
        _fail("invalid output-parameter semantics")
    if (
        not isinstance(output["name"], str)
        or _PATH_RE.fullmatch(output["name"]) is None
        or output["name"] in {"model", "messages", "reasoning_effort", "response_format", "tools", "tool_choice"}
        or output["includes_reasoning"] is not True
        or output["includes_tool_calls"] is not True
        or output["includes_final_output"] is not True
    ):
        _fail("unknown reasoning-inclusive output semantics")
    usage = value["usage"]
    usage_fields = {
        "input_tokens_path", "output_tokens_path", "output_includes_reasoning",
        "reported_cost_path", "reported_cost_kind", "reported_cost_scope",
    }
    if not isinstance(usage, Mapping) or set(usage) != usage_fields:
        _fail("invalid provider usage semantics")
    if usage["output_includes_reasoning"] is not True:
        _fail("unknown provider reasoning usage semantics")
    input_path = _path(usage["input_tokens_path"], "input usage")
    output_path = _path(usage["output_tokens_path"], "output usage")
    if input_path == output_path:
        _fail("input and output usage accounting paths must differ")
    cost_path_value = usage["reported_cost_path"]
    cost_kind = usage["reported_cost_kind"]
    cost_scope = usage["reported_cost_scope"]
    if cost_path_value is None and cost_kind is None and cost_scope is None:
        cost_path = None
    elif cost_kind in {"account_charge", "nonincremental_estimate"} and cost_scope == "request":
        cost_path = _path(cost_path_value, "reported cost")
    else:
        _fail("unknown provider-reported cost semantics")
    max_tool_result = _positive(value["max_tool_result_utf8_bytes"], "maximum tool-result size")
    if max_tool_result > 16 * 1024 * 1024:
        _fail("maximum tool-result size exceeds controller limit")
    return {
        "schema": MECHANISM_SCHEMA,
        "route_sha256": _sha(value["route_sha256"], "route"),
        "provider": provider,
        "serializer": SERIALIZER,
        "counter_command_sha256": _sha(value["counter_command_sha256"], "counter command"),
        "counter_semantics_sha256": _sha(value["counter_semantics_sha256"], "counter semantics"),
        "input_semantics": INPUT_SEMANTICS,
        "output_parameter": dict(output),
        "usage": {
            "input_tokens_path": input_path,
            "output_tokens_path": output_path,
            "output_includes_reasoning": True,
            "reported_cost_path": cost_path,
            "reported_cost_kind": cost_kind,
            "reported_cost_scope": cost_scope,
        },
        "cache_billing": CACHE_BILLING,
        "max_tool_result_utf8_bytes": max_tool_result,
    }


def _validate_provider_bound_mechanism(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate conservative documented provider bounds, explicitly not tokenization."""

    if set(value) != _PROVIDER_BOUND_FIELDS:
        _fail("invalid provider-bound request-budget mechanism fields")
    provider = value["provider"]
    if not isinstance(provider, str) or not provider.strip():
        _fail("invalid request-budget provider")
    if value["serializer"] != SERIALIZER or value["cache_billing"] != CACHE_BILLING:
        _fail("unknown provider-bound serialization or cache semantics")
    input_bound = value["input_bound"]
    input_fields = {
        "kind", "max_input_tokens_per_request", "max_requests_per_segment",
        "includes_hidden_provider_framing", "evidence_sha256",
    }
    if not isinstance(input_bound, Mapping) or set(input_bound) != input_fields:
        _fail("invalid provider input upper bound")
    if (
        input_bound["kind"] != "provider_context_upper_bound"
        or input_bound["includes_hidden_provider_framing"] is not True
    ):
        _fail("provider input bound must cover hidden provider framing")
    output = value["output_parameter"]
    output_fields = {
        "name", "max_tokens_per_request", "includes_reasoning", "includes_tool_calls",
        "includes_final_output", "evidence_sha256",
    }
    if not isinstance(output, Mapping) or set(output) != output_fields:
        _fail("invalid provider output upper bound")
    if (
        not isinstance(output["name"], str)
        or _PATH_RE.fullmatch(output["name"]) is None
        or output["name"] in {"model", "messages", "reasoning_effort", "response_format", "tools", "tool_choice"}
        or output["includes_reasoning"] is not True
        or output["includes_tool_calls"] is not True
        or output["includes_final_output"] is not True
    ):
        _fail("unknown reasoning-inclusive output semantics")
    usage = value["usage"]
    usage_fields = {
        "input_tokens_path", "output_tokens_path", "output_includes_reasoning",
        "reported_cost_path", "reported_cost_kind", "reported_cost_scope",
    }
    if not isinstance(usage, Mapping) or set(usage) != usage_fields:
        _fail("invalid provider usage semantics")
    if usage["output_includes_reasoning"] is not True:
        _fail("unknown provider reasoning usage semantics")
    input_path = _path(usage["input_tokens_path"], "input usage")
    output_path = _path(usage["output_tokens_path"], "output usage")
    if input_path == output_path:
        _fail("input and output usage accounting paths must differ")
    if (
        usage["reported_cost_path"] is None
        and usage["reported_cost_kind"] is None
        and usage["reported_cost_scope"] is None
    ):
        cost_path = cost_kind = cost_scope = None
    elif usage["reported_cost_kind"] == "account_charge" and usage["reported_cost_scope"] == "request":
        cost_path = _path(usage["reported_cost_path"], "reported cost")
        cost_kind, cost_scope = "account_charge", "request"
    else:
        _fail("provider-bound cost semantics must be absent or an authoritative per-request account charge")
    max_tool_result = _positive(value["max_tool_result_utf8_bytes"], "maximum tool-result size")
    if max_tool_result > 16 * 1024 * 1024:
        _fail("maximum tool-result size exceeds controller limit")
    return {
        "schema": PROVIDER_BOUND_MECHANISM_SCHEMA,
        "route_sha256": _sha(value["route_sha256"], "route"),
        "provider": provider,
        "serializer": SERIALIZER,
        "input_bound": {
            "kind": "provider_context_upper_bound",
            "max_input_tokens_per_request": _positive(
                input_bound["max_input_tokens_per_request"], "maximum provider input tokens"
            ),
            "max_requests_per_segment": _positive(
                input_bound["max_requests_per_segment"], "maximum segment requests"
            ),
            "includes_hidden_provider_framing": True,
            "evidence_sha256": _sha(input_bound["evidence_sha256"], "provider context evidence"),
        },
        "output_parameter": {
            "name": output["name"],
            "max_tokens_per_request": _positive(
                output["max_tokens_per_request"], "maximum reasoning-inclusive output tokens"
            ),
            "includes_reasoning": True,
            "includes_tool_calls": True,
            "includes_final_output": True,
            "evidence_sha256": _sha(output["evidence_sha256"], "provider output evidence"),
        },
        "usage": {
            "input_tokens_path": input_path,
            "output_tokens_path": output_path,
            "output_includes_reasoning": True,
            "reported_cost_path": cost_path,
            "reported_cost_kind": cost_kind,
            "reported_cost_scope": cost_scope,
        },
        "cache_billing": CACHE_BILLING,
        "max_tool_result_utf8_bytes": max_tool_result,
        "pricing_evidence_sha256": _sha(value["pricing_evidence_sha256"], "pricing evidence"),
        "backend_identity_evidence_sha256": _sha(
            value["backend_identity_evidence_sha256"], "backend identity evidence"
        ),
    }


def _validate_usage_bound_mechanism(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate final-usage settlement without reinterpreting v2 receipts."""

    if set(value) != _USAGE_BOUND_FIELDS:
        _fail("invalid usage-bound request-budget mechanism fields")
    base_value = {key: item for key, item in value.items() if key != "usage_upper_bound"}
    base_value["schema"] = PROVIDER_BOUND_MECHANISM_SCHEMA
    base = _validate_provider_bound_mechanism(base_value)
    if any(
        base["usage"][field] is not None
        for field in ("reported_cost_path", "reported_cost_kind", "reported_cost_scope")
    ):
        _fail("usage-bound settlement requires explicit absence of inline account charge")
    upper = value["usage_upper_bound"]
    fields = {
        "mode", "max_billable_requests_per_segment", "rounding_scope",
        "rounding_quantum_micro_usd", "maximum_additional_fees_micro_usd",
        "usage_semantics_evidence_sha256", "additional_fees_evidence_sha256",
        "prices_are_maximum_for_backend", "usage_is_final", "automatic_paid_retries",
        "usage_scope", "input_usage_includes_all_billable_categories",
    }
    if not isinstance(upper, Mapping) or set(upper) != fields:
        _fail("invalid conservative final-usage upper-bound fields")
    if upper["mode"] != "conservative_final_usage_upper_bound":
        _fail("unsupported final-usage settlement mode")
    if upper["rounding_scope"] != "per_request_component":
        _fail("usage-bound rounding must apply to each request component")
    if upper["usage_scope"] != "request":
        _fail("usage-bound settlement requires per-request usage")
    if (
        upper["prices_are_maximum_for_backend"] is not True
        or upper["usage_is_final"] is not True
        or upper["automatic_paid_retries"] is not False
        or upper["input_usage_includes_all_billable_categories"] is not True
    ):
        _fail("usage-bound settlement lacks required price, finality, or retry guarantees")
    request_count = _positive(
        upper["max_billable_requests_per_segment"], "maximum billable request count"
    )
    if request_count != base["input_bound"]["max_requests_per_segment"]:
        _fail("usage-bound billable request count differs from provider input bound")
    return {
        **base,
        "schema": USAGE_BOUND_MECHANISM_SCHEMA,
        "usage_upper_bound": {
            "mode": "conservative_final_usage_upper_bound",
            "max_billable_requests_per_segment": request_count,
            "rounding_scope": "per_request_component",
            "rounding_quantum_micro_usd": _positive(
                upper["rounding_quantum_micro_usd"], "rounding quantum"
            ),
            "maximum_additional_fees_micro_usd": _positive_or_zero(
                upper["maximum_additional_fees_micro_usd"], "maximum additional fees"
            ),
            "usage_semantics_evidence_sha256": _sha(
                upper["usage_semantics_evidence_sha256"], "usage semantics evidence"
            ),
            "additional_fees_evidence_sha256": _sha(
                upper["additional_fees_evidence_sha256"], "additional fees evidence"
            ),
            "prices_are_maximum_for_backend": True,
            "usage_is_final": True,
            "automatic_paid_retries": False,
            "usage_scope": "request",
            "input_usage_includes_all_billable_categories": True,
        },
    }


def mechanism_sha256(value: Any) -> str:
    return digest(validate_mechanism(value))


def _ceil_cost(rate: int, tokens: int) -> int:
    return (rate * tokens + 999_999) // 1_000_000


def _ceil_cost_quantum(rate: int, tokens: int, quantum_micro_usd: int) -> int:
    denominator = 1_000_000 * quantum_micro_usd
    return ((rate * tokens + denominator - 1) // denominator) * quantum_micro_usd


def _maximum_segment_charge(billing: Mapping[str, Any]) -> int:
    return (
        _ceil_cost(billing["input_micro_usd_per_million_tokens"], billing["max_total_input_tokens"])
        + _ceil_cost(billing["output_micro_usd_per_million_tokens"], billing["max_total_output_tokens"])
        + billing["max_tool_rounds"] * billing["tool_round_micro_usd"]
    )


def _maximum_usage_bound_charge(mechanism: Mapping[str, Any], billing: Mapping[str, Any]) -> int:
    upper = mechanism["usage_upper_bound"]
    requests = upper["max_billable_requests_per_segment"]
    quantum = upper["rounding_quantum_micro_usd"]
    return (
        requests * _ceil_cost_quantum(
            billing["input_micro_usd_per_million_tokens"],
            mechanism["input_bound"]["max_input_tokens_per_request"],
            quantum,
        )
        + requests * _ceil_cost_quantum(
            billing["output_micro_usd_per_million_tokens"],
            mechanism["output_parameter"]["max_tokens_per_request"],
            quantum,
        )
        + billing["max_tool_rounds"] * billing["tool_round_micro_usd"]
        + upper["maximum_additional_fees_micro_usd"]
    )


def request_budget_attempt_id(candidate_attempt_id: str) -> str:
    if not isinstance(candidate_attempt_id, str) or not candidate_attempt_id:
        raise ExamError("invalid candidate attempt identity for request budget")
    return "budget-" + digest({"candidate_attempt_id": candidate_attempt_id})[:48]


def verify_request_budget_evidence(evidence, route, config, candidate_attempt_id, candidate_result) -> None:
    """Authenticate one retained budget receipt and its candidate-result link."""

    if not isinstance(evidence, Mapping) or evidence.get("complete") is not True:
        raise ExamError("request-budget evidence is not finalized")
    if evidence.get("attempt_id") != request_budget_attempt_id(candidate_attempt_id):
        raise ExamError("request-budget attempt identity mismatch")
    metadata = evidence.get("metadata")
    base_fields = {
        "denominator", "candidate_attempt_id", "route_sha256", "account_sha256",
        "mechanism_sha256", "billing_kind", "credit_commitment_micro_usd",
        "maximum_segment_charge_micro_usd",
    }
    if not isinstance(metadata, Mapping):
        raise ExamError("request-budget metadata is invalid")
    provider_bound = "shared_reservation_sha256" in metadata
    usage_bound = metadata.get("request_budget_mechanism_schema") == USAGE_BOUND_MECHANISM_SCHEMA
    fields = base_fields | ({"shared_reservation_sha256"} if provider_bound else set())
    if usage_bound:
        fields.add("request_budget_mechanism_schema")
    if set(metadata) != fields or metadata["denominator"] != 0:
        raise ExamError("request-budget metadata is invalid")
    if provider_bound and _SHA256_RE.fullmatch(str(metadata["shared_reservation_sha256"])) is None:
        raise ExamError("request-budget shared reservation identity is invalid")
    if (
        metadata["candidate_attempt_id"] != candidate_attempt_id
        or metadata["route_sha256"] != route["route_sha256"]
        or metadata["mechanism_sha256"] != route["request_budget_mechanism_sha256"]
        or metadata["billing_kind"] != route["billing"]["kind"]
    ):
        raise ExamError("request-budget metadata binding mismatch")
    if not isinstance(metadata["account_sha256"], str) or _SHA256_RE.fullmatch(metadata["account_sha256"]) is None:
        raise ExamError("request-budget account identity is invalid")
    maximum_charge = _maximum_segment_charge(route["billing"])
    if metadata["maximum_segment_charge_micro_usd"] != maximum_charge:
        raise ExamError("request-budget maximum charge drift")
    expected_credit = maximum_charge if route["billing"]["kind"] == "existing_credit" else 0
    if metadata["credit_commitment_micro_usd"] != expected_credit:
        raise ExamError("request-budget credit commitment drift")
    if candidate_result.get("failure_reason") == "interrupted":
        if evidence.get("terminal_status") != "interrupted":
            raise ExamError("interrupted candidate lacks interrupted request budget")
        return
    expected_terminal = "completed" if candidate_result.get("status") == "ok" else "failed"
    if evidence.get("terminal_status") != expected_terminal:
        raise ExamError("candidate and request-budget status differ")
    result = evidence.get("result")
    result_fields = {
        "schema", "status", "mechanism_sha256", "rounds_committed", "input_tokens_committed",
        "output_tokens_reserved", "input_tokens_observed",
        "output_tokens_including_reasoning_observed", "tool_calls_observed",
        "formula_charge_micro_usd", "provider_reported_charge_micro_usd", "reported_cost_kind",
    }
    if provider_bound:
        result_fields.add("shared_settlement_sha256")
    if usage_bound:
        result_fields.update({
            "settlement_kind", "ordered_final_usage", "ordered_final_usage_sha256",
            "rounding_scope", "rounding_quantum_micro_usd",
            "maximum_additional_fees_micro_usd", "pricing_evidence_sha256",
            "backend_identity_evidence_sha256", "usage_semantics_evidence_sha256",
            "additional_fees_evidence_sha256", "charge_upper_bound_micro_usd",
        })
    if not isinstance(result, Mapping) or set(result) != result_fields:
        raise ExamError("request-budget result is invalid")
    if (
        result["schema"] != (
            "ukrainian-llm-eval.request-budget-receipt.v3"
            if usage_bound else (
                "ukrainian-llm-eval.request-budget-receipt.v2"
                if provider_bound else "ukrainian-llm-eval.request-budget-receipt.v1"
            )
        )
        or result["status"] != expected_terminal
        or result["mechanism_sha256"] != route["request_budget_mechanism_sha256"]
    ):
        raise ExamError("request-budget result binding mismatch")
    count_fields = result_fields - {
        "schema", "status", "mechanism_sha256", "provider_reported_charge_micro_usd", "reported_cost_kind",
        "shared_settlement_sha256",
        "settlement_kind", "ordered_final_usage", "ordered_final_usage_sha256",
        "rounding_scope", "rounding_quantum_micro_usd", "maximum_additional_fees_micro_usd",
        "pricing_evidence_sha256", "backend_identity_evidence_sha256",
        "usage_semantics_evidence_sha256", "additional_fees_evidence_sha256",
        "charge_upper_bound_micro_usd",
    }
    counts = {field: result[field] for field in count_fields}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ExamError("request-budget result counts are invalid")
    provider_charge = result["provider_reported_charge_micro_usd"]
    if provider_charge is not None and (type(provider_charge) is not int or provider_charge < 0):
        raise ExamError("request-budget provider charge is invalid")
    if result["reported_cost_kind"] not in {None, "account_charge", "nonincremental_estimate"}:
        raise ExamError("request-budget reported cost kind is invalid")
    if provider_bound:
        settlement = result["shared_settlement_sha256"]
        if settlement is not None and _SHA256_RE.fullmatch(str(settlement)) is None:
            raise ExamError("request-budget shared settlement identity is invalid")
    if usage_bound:
        _verify_usage_bound_result(result, route, maximum_charge)
    expected_formula_charge = (
        _ceil_cost(route["billing"]["input_micro_usd_per_million_tokens"], result["input_tokens_observed"])
        + _ceil_cost(
            route["billing"]["output_micro_usd_per_million_tokens"],
            result["output_tokens_including_reasoning_observed"],
        )
        + result["tool_calls_observed"] * route["billing"]["tool_round_micro_usd"]
    )
    if result["formula_charge_micro_usd"] != expected_formula_charge:
        raise ExamError("request-budget formula charge is invalid")
    if (
        result["reported_cost_kind"] == "account_charge"
        and provider_charge is not None
        and provider_charge > maximum_charge
    ):
        raise ExamError("request-budget provider charge exceeds frozen maximum")
    if (
        result["rounds_committed"] > config["max_tool_calls"] + 1
        or result["input_tokens_committed"] > route["billing"]["max_total_input_tokens"]
        or result["output_tokens_reserved"] > route["billing"]["max_total_output_tokens"]
        or result["output_tokens_reserved"] != result["rounds_committed"] * config["max_output_tokens"]
        or result["input_tokens_observed"] > result["input_tokens_committed"]
        or result["output_tokens_including_reasoning_observed"] > result["output_tokens_reserved"]
        or result["tool_calls_observed"] > config["max_tool_calls"]
        or result["tool_calls_observed"] > route["billing"]["max_tool_rounds"]
    ):
        raise ExamError("request-budget result exceeds frozen limits")
    identity = candidate_result.get("identity")
    if not isinstance(identity, Mapping) or identity.get("request_budget_receipt_sha256") != digest(evidence):
        raise ExamError("candidate result lacks its request-budget receipt binding")


def _verify_usage_bound_result(
    result: Mapping[str, Any], route: Mapping[str, Any], maximum_charge: int
) -> None:
    if result["settlement_kind"] != "conservative_final_usage_upper_bound":
        raise ExamError("request-budget usage settlement kind is invalid")
    if result["reported_cost_kind"] is not None or result["provider_reported_charge_micro_usd"] is not None:
        raise ExamError("usage-bound receipt must not claim an authoritative account charge")
    if result["rounding_scope"] != "per_request_component":
        raise ExamError("request-budget usage-bound rounding scope is invalid")
    quantum = result["rounding_quantum_micro_usd"]
    if type(quantum) is not int or quantum <= 0:
        raise ExamError("request-budget usage-bound rounding quantum is invalid")
    evidence_fields = {
        "pricing_evidence_sha256", "backend_identity_evidence_sha256",
        "usage_semantics_evidence_sha256", "additional_fees_evidence_sha256",
    }
    if any(_SHA256_RE.fullmatch(str(result[field])) is None for field in evidence_fields):
        raise ExamError("request-budget usage-bound evidence identity is invalid")
    if result["pricing_evidence_sha256"] != route["pricing_evidence_sha256"]:
        raise ExamError("request-budget usage-bound pricing evidence drift")
    entries = result["ordered_final_usage"]
    if not isinstance(entries, list) or digest(entries) != result["ordered_final_usage_sha256"]:
        raise ExamError("request-budget ordered final usage is invalid")
    fields = {
        "round", "request_sha256", "input_tokens", "reasoning_inclusive_output_tokens",
        "tool_calls", "input_charge_upper_bound_micro_usd",
        "output_charge_upper_bound_micro_usd", "tool_charge_upper_bound_micro_usd",
        "request_charge_upper_bound_micro_usd",
    }
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping) or set(entry) != fields or entry["round"] != index:
            raise ExamError("request-budget ordered final usage entry is invalid")
        if _SHA256_RE.fullmatch(str(entry["request_sha256"])) is None:
            raise ExamError("request-budget ordered final usage request identity is invalid")
        counts = {key: entry[key] for key in fields - {"request_sha256"}}
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise ExamError("request-budget ordered final usage counts are invalid")
        if entry["request_charge_upper_bound_micro_usd"] != (
            entry["input_charge_upper_bound_micro_usd"]
            + entry["output_charge_upper_bound_micro_usd"]
            + entry["tool_charge_upper_bound_micro_usd"]
        ):
            raise ExamError("request-budget per-request charge upper bound is invalid")
        billing = route["billing"]
        if (
            entry["input_charge_upper_bound_micro_usd"]
            != _ceil_cost_quantum(
                billing["input_micro_usd_per_million_tokens"], entry["input_tokens"], quantum
            )
            or entry["output_charge_upper_bound_micro_usd"]
            != _ceil_cost_quantum(
                billing["output_micro_usd_per_million_tokens"],
                entry["reasoning_inclusive_output_tokens"],
                quantum,
            )
            or entry["tool_charge_upper_bound_micro_usd"]
            != entry["tool_calls"] * billing["tool_round_micro_usd"]
        ):
            raise ExamError("request-budget usage-derived component bound is invalid")
    if (
        len(entries) > result["rounds_committed"]
        or sum(item["input_tokens"] for item in entries) != result["input_tokens_observed"]
        or sum(item["reasoning_inclusive_output_tokens"] for item in entries)
        != result["output_tokens_including_reasoning_observed"]
        or sum(item["tool_calls"] for item in entries) != result["tool_calls_observed"]
    ):
        raise ExamError("request-budget ordered final usage totals are invalid")
    fees = result["maximum_additional_fees_micro_usd"]
    if type(fees) is not int or fees < 0:
        raise ExamError("request-budget additional fee bound is invalid")
    charge = result["charge_upper_bound_micro_usd"]
    settled = result["shared_settlement_sha256"] is not None
    if settled:
        expected = sum(item["request_charge_upper_bound_micro_usd"] for item in entries) + fees
        if len(entries) != result["rounds_committed"] or charge != expected or charge > maximum_charge:
            raise ExamError("request-budget final usage charge upper bound is invalid")
    elif charge is not None:
        raise ExamError("unsettled request budget claims a final usage charge upper bound")


def _lookup(value: Any, path: list[str]) -> int:
    current = value
    for component in path:
        if not isinstance(current, Mapping) or component not in current:
            _fail("provider usage is missing required accounting fields")
        current = current[component]
    if type(current) is not int or current < 0:
        _fail("provider usage accounting is invalid")
    return current


def _lookup_value(value: Any, path: list[str]) -> Any:
    current = value
    for component in path:
        if not isinstance(current, Mapping) or component not in current:
            _fail("provider usage is missing required accounting fields")
        current = current[component]
    return current


def _reported_micro_usd(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _fail("provider-reported cost is invalid")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise RequestBudgetError("provider-reported cost is invalid") from exc
    if not amount.is_finite() or amount < 0:
        _fail("provider-reported cost is invalid")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _serialize(payload: Mapping[str, Any]) -> bytes:
    return canonical(dict(payload)).encode("utf-8") + b"\n"


class RequestBudget:
    """One segment's durable, cumulative pre-request budget."""

    def __init__(
        self, route, config, mechanism, counter_command, attempt, *, maximum_charge: int,
        shared_ledger=None, shared_reservation_id: str | None = None,
    ):
        self.route = route
        self.config = config
        self.mechanism = mechanism
        self.counter_command = counter_command
        self.attempt = attempt
        self.maximum_charge = maximum_charge
        self.shared_ledger = shared_ledger
        self.shared_reservation_id = shared_reservation_id
        self.rounds = 0
        self.input_tokens = 0
        self.output_tokens_reserved = 0
        self.observed_input_tokens = 0
        self.observed_output_tokens = 0
        self.tool_calls = 0
        self.formula_charge = 0
        self.provider_reported_charge: int | None = None
        self.authoritative_charge_rounds = 0
        self.ordered_final_usage: list[dict[str, Any]] = []
        self._pending: dict[str, Any] | None = None
        self._closed = False

    @property
    def output_parameter_name(self) -> str:
        return self.mechanism["output_parameter"]["name"]

    def _reject_with_evidence(self, reason: str, payload: Mapping[str, Any]) -> None:
        self.attempt.append(
            "request_budget_rejected",
            {"reason": reason, "round": self.rounds + (1 if self._pending is None else 0), **dict(payload)},
        )
        _fail(reason)

    def commit_request(self, payload: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        """Count, reserve, and durably commit one exact body before transport."""

        if self._closed or self._pending is not None:
            _fail("request-budget lifecycle violation")
        if self.rounds >= self.config["max_tool_calls"] + 1:
            _fail("request round exceeds frozen tool-call bound")
        if payload.get(self.output_parameter_name) != self.config["max_output_tokens"]:
            _fail("provider output parameter differs from frozen semantics")
        request_bytes = _serialize(payload)
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        mechanism_schema = self.mechanism["schema"]
        provider_bound = mechanism_schema in {
            PROVIDER_BOUND_MECHANISM_SCHEMA, USAGE_BOUND_MECHANISM_SCHEMA,
        }
        if provider_bound:
            counted = self.mechanism["input_bound"]["max_input_tokens_per_request"]
        else:
            process = invoke_admission(self.counter_command, dict(payload))
            self.attempt.append(
                "request_counter_process", {key: value for key, value in process.items() if key != "stdout"}
            )
            if (
                process.get("status") != "success"
                or process.get("command_identity_sha256") != self.mechanism["counter_command_sha256"]
            ):
                _fail("trusted request counter failed")
            try:
                result = json.loads(
                    process["stdout"].decode("utf-8"),
                    object_pairs_hook=_duplicate_rejecting_pairs,
                    parse_constant=_reject_json_constant,
                )
            except (KeyError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise RequestBudgetError("trusted request counter returned invalid evidence") from exc
            fields = {"schema", "request_sha256", "counter_semantics_sha256", "input_tokens"}
            if not isinstance(result, Mapping) or set(result) != fields:
                _fail("trusted request counter returned invalid evidence")
            if (
                result["schema"] != COUNTER_RESULT_SCHEMA
                or result["request_sha256"] != request_sha256
                or result["counter_semantics_sha256"] != self.mechanism["counter_semantics_sha256"]
            ):
                _fail("trusted request counter identity mismatch")
            counted = _positive(result["input_tokens"], "counted input tokens")
        next_input = self.input_tokens + counted
        next_output = self.output_tokens_reserved + self.config["max_output_tokens"]
        billing = self.route["billing"]
        if next_input > billing["max_total_input_tokens"]:
            self._reject_with_evidence(
                "cumulative input reservation exceeded",
                {"request_sha256": request_sha256, "cumulative_input_tokens": next_input},
            )
        if next_output > billing["max_total_output_tokens"]:
            self._reject_with_evidence(
                "cumulative reasoning-inclusive output reservation exceeded",
                {"request_sha256": request_sha256, "cumulative_output_tokens_reserved": next_output},
            )
        committed_charge = (
            _ceil_cost(billing["input_micro_usd_per_million_tokens"], next_input)
            + _ceil_cost(billing["output_micro_usd_per_million_tokens"], next_output)
            + self.tool_calls * billing["tool_round_micro_usd"]
        )
        if committed_charge > self.maximum_charge:
            self._reject_with_evidence(
                "request commitment exceeds frozen segment charge",
                {"request_sha256": request_sha256, "committed_charge_micro_usd": committed_charge},
            )
        entry = {
            "schema": {
                MECHANISM_SCHEMA: "ukrainian-llm-eval.request-budget-commitment.v1",
                PROVIDER_BOUND_MECHANISM_SCHEMA: "ukrainian-llm-eval.request-budget-commitment.v2",
                USAGE_BOUND_MECHANISM_SCHEMA: "ukrainian-llm-eval.request-budget-commitment.v3",
            }[mechanism_schema],
            "round": self.rounds + 1,
            "request_sha256": request_sha256,
            "request_utf8_bytes": len(request_bytes),
            "input_tokens": counted,
            "cumulative_input_tokens": next_input,
            "cumulative_output_tokens_reserved": next_output,
            "cumulative_tool_calls": self.tool_calls,
            "committed_charge_micro_usd": committed_charge,
            "mechanism_sha256": digest(self.mechanism),
        }
        if provider_bound:
            entry["input_bound_kind"] = "provider_context_upper_bound"
            entry["input_bound_evidence_sha256"] = self.mechanism["input_bound"]["evidence_sha256"]
        else:
            entry["counter_command_sha256"] = self.mechanism["counter_command_sha256"]
            entry["counter_semantics_sha256"] = self.mechanism["counter_semantics_sha256"]
        self.attempt.append("request_budget_committed", entry)
        self.rounds += 1
        self.input_tokens = next_input
        self.output_tokens_reserved = next_output
        self._pending = entry
        return request_bytes, copy.deepcopy(entry)

    def observe(self, usage: Mapping[str, Any], *, tool_calls: int) -> dict[str, Any]:
        """Bind exact provider usage before any subsequent request or tool call."""

        if self._closed or self._pending is None:
            _fail("request-budget observation without a committed request")
        if type(tool_calls) is not int or tool_calls < 0:
            _fail("invalid observed tool-call count")
        try:
            observed_input = _lookup(usage, self.mechanism["usage"]["input_tokens_path"])
            observed_output = _lookup(usage, self.mechanism["usage"]["output_tokens_path"])
        except RequestBudgetError:
            self._reject_with_evidence(
                "provider usage is missing required accounting fields",
                {"request_sha256": self._pending["request_sha256"]},
            )
        next_observed_input = self.observed_input_tokens + observed_input
        next_observed_output = self.observed_output_tokens + observed_output
        next_tools = self.tool_calls + tool_calls
        billing = self.route["billing"]
        if observed_input > self._pending["input_tokens"] or next_observed_input > billing["max_total_input_tokens"]:
            self._reject_with_evidence(
                "observed input token count exceeds committed bound",
                {"request_sha256": self._pending["request_sha256"], "observed_input_tokens": observed_input,
                 "cumulative_observed_input_tokens": next_observed_input},
            )
        if (
            observed_output > self.config["max_output_tokens"]
            or next_observed_output > billing["max_total_output_tokens"]
        ):
            self._reject_with_evidence(
                "observed reasoning-inclusive output exceeds committed bound",
                {"request_sha256": self._pending["request_sha256"], "observed_output_tokens": observed_output,
                 "cumulative_observed_output_tokens": next_observed_output},
            )
        if next_tools > self.config["max_tool_calls"] or next_tools > billing["max_tool_rounds"]:
            self._reject_with_evidence(
                "observed tool calls exceed frozen bound",
                {"request_sha256": self._pending["request_sha256"], "cumulative_tool_calls": next_tools},
            )
        observed_charge = (
            _ceil_cost(billing["input_micro_usd_per_million_tokens"], next_observed_input)
            + _ceil_cost(billing["output_micro_usd_per_million_tokens"], next_observed_output)
            + next_tools * billing["tool_round_micro_usd"]
        )
        if observed_charge > self.maximum_charge:
            self._reject_with_evidence(
                "observed provider charge exceeds frozen segment charge",
                {"request_sha256": self._pending["request_sha256"],
                 "formula_charge_micro_usd": observed_charge},
            )
        cost_path = self.mechanism["usage"]["reported_cost_path"]
        provider_charge = None
        cumulative_provider_charge = self.provider_reported_charge
        if cost_path is not None:
            try:
                provider_charge = _reported_micro_usd(_lookup_value(usage, cost_path))
            except RequestBudgetError:
                self._reject_with_evidence(
                    "provider-reported cost is invalid",
                    {"request_sha256": self._pending["request_sha256"]},
                )
            cumulative_provider_charge = (self.provider_reported_charge or 0) + provider_charge
            if (
                self.mechanism["usage"]["reported_cost_kind"] == "account_charge"
                and cumulative_provider_charge > self.maximum_charge
            ):
                self._reject_with_evidence(
                    "provider-reported charge exceeds frozen segment charge",
                    {"request_sha256": self._pending["request_sha256"],
                     "cumulative_provider_reported_charge_micro_usd": cumulative_provider_charge},
                )
        observation = {
            "schema": "ukrainian-llm-eval.request-budget-observation.v1",
            "round": self.rounds,
            "request_sha256": self._pending["request_sha256"],
            "input_tokens": observed_input,
            "output_tokens_including_reasoning": observed_output,
            "cumulative_input_tokens": next_observed_input,
            "cumulative_output_tokens_including_reasoning": next_observed_output,
            "cumulative_tool_calls": next_tools,
            "observed_charge_micro_usd": observed_charge,
            "provider_reported_charge_micro_usd": provider_charge,
            "cumulative_provider_reported_charge_micro_usd": cumulative_provider_charge,
        }
        if self.mechanism["schema"] == USAGE_BOUND_MECHANISM_SCHEMA:
            upper = self.mechanism["usage_upper_bound"]
            quantum = upper["rounding_quantum_micro_usd"]
            input_bound = _ceil_cost_quantum(
                billing["input_micro_usd_per_million_tokens"], observed_input, quantum
            )
            output_bound = _ceil_cost_quantum(
                billing["output_micro_usd_per_million_tokens"], observed_output, quantum
            )
            tool_bound = tool_calls * billing["tool_round_micro_usd"]
            usage_entry = {
                "round": self.rounds,
                "request_sha256": self._pending["request_sha256"],
                "input_tokens": observed_input,
                "reasoning_inclusive_output_tokens": observed_output,
                "tool_calls": tool_calls,
                "input_charge_upper_bound_micro_usd": input_bound,
                "output_charge_upper_bound_micro_usd": output_bound,
                "tool_charge_upper_bound_micro_usd": tool_bound,
                "request_charge_upper_bound_micro_usd": input_bound + output_bound + tool_bound,
            }
            observation["conservative_final_usage"] = usage_entry
        self.attempt.append("request_budget_observed", observation)
        if self.mechanism["schema"] == USAGE_BOUND_MECHANISM_SCHEMA:
            self.ordered_final_usage.append(usage_entry)
        self.observed_input_tokens = next_observed_input
        self.observed_output_tokens = next_observed_output
        self.tool_calls = next_tools
        self.formula_charge = observed_charge
        self.provider_reported_charge = cumulative_provider_charge
        if provider_charge is not None and self.mechanism["usage"]["reported_cost_kind"] == "account_charge":
            self.authoritative_charge_rounds += 1
        self._pending = None
        return copy.deepcopy(observation)

    def serialize_tool_result(self, result: Any) -> str:
        """Serialize once and reject an oversized result without truncation."""

        text = canonical(result)
        raw = text.encode("utf-8")
        if len(raw) > self.mechanism["max_tool_result_utf8_bytes"]:
            self.attempt.append(
                "tool_result_rejected",
                {"utf8_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            )
            _fail("tool result exceeds frozen request-budget size")
        return text

    def finalize(self, status: str) -> dict[str, Any]:
        if self._closed:
            _fail("request budget already finalized")
        if status == "completed" and self._pending is not None:
            _fail("successful request budget lacks provider observation")
        mechanism_schema = self.mechanism["schema"]
        provider_bound = mechanism_schema in {
            PROVIDER_BOUND_MECHANISM_SCHEMA, USAGE_BOUND_MECHANISM_SCHEMA,
        }
        usage_bound = mechanism_schema == USAGE_BOUND_MECHANISM_SCHEMA
        shared_settlement = None
        if self.shared_ledger is not None:
            authoritative = not usage_bound and (self.rounds == 0 or (
                self._pending is None and self.authoritative_charge_rounds == self.rounds
            ))
            if authoritative:
                charge = self.provider_reported_charge or 0
                settlement_evidence = digest({
                    "reservation_id": self.shared_reservation_id,
                    "rounds": self.rounds,
                    "authoritative_account_charge_micro_usd": charge,
                    "mechanism_sha256": digest(self.mechanism),
                })
                shared_settlement = self.shared_ledger.settle(
                    self.shared_reservation_id,
                    charged_micro_usd=charge,
                    evidence_sha256=settlement_evidence,
                )
            elif usage_bound and (
                self.rounds == 0
                or (self._pending is None and len(self.ordered_final_usage) == self.rounds)
            ):
                upper = self.mechanism["usage_upper_bound"]
                charge = (
                    sum(item["request_charge_upper_bound_micro_usd"] for item in self.ordered_final_usage)
                    + upper["maximum_additional_fees_micro_usd"]
                )
                if charge > self.maximum_charge:
                    _fail("final usage charge upper bound exceeds reserved worst case")
                settlement_body = {
                    "schema": "ukrainian-llm-eval.usage-bound-settlement-evidence.v1",
                    "settlement_kind": "conservative_final_usage_upper_bound",
                    "reservation_id": self.shared_reservation_id,
                    "provider": self.mechanism["provider"],
                    "mechanism_sha256": digest(self.mechanism),
                    "pricing_evidence_sha256": self.mechanism["pricing_evidence_sha256"],
                    "backend_identity_evidence_sha256": self.mechanism["backend_identity_evidence_sha256"],
                    "input_bound_evidence_sha256": self.mechanism["input_bound"]["evidence_sha256"],
                    "output_bound_evidence_sha256": self.mechanism["output_parameter"]["evidence_sha256"],
                    "usage_semantics_evidence_sha256": upper["usage_semantics_evidence_sha256"],
                    "additional_fees_evidence_sha256": upper["additional_fees_evidence_sha256"],
                    "rounding_scope": upper["rounding_scope"],
                    "rounding_quantum_micro_usd": upper["rounding_quantum_micro_usd"],
                    "input_micro_usd_per_million_tokens": (
                        self.route["billing"]["input_micro_usd_per_million_tokens"]
                    ),
                    "output_micro_usd_per_million_tokens": (
                        self.route["billing"]["output_micro_usd_per_million_tokens"]
                    ),
                    "tool_round_micro_usd": self.route["billing"]["tool_round_micro_usd"],
                    "maximum_additional_fees_micro_usd": upper["maximum_additional_fees_micro_usd"],
                    "ordered_final_usage": self.ordered_final_usage,
                    "charge_upper_bound_micro_usd": charge,
                }
                shared_settlement = self.shared_ledger.settle(
                    self.shared_reservation_id,
                    charged_micro_usd=charge,
                    evidence_sha256=digest(settlement_body),
                    settlement_kind="conservative_final_usage_upper_bound",
                )
        result = {
            "schema": {
                MECHANISM_SCHEMA: "ukrainian-llm-eval.request-budget-receipt.v1",
                PROVIDER_BOUND_MECHANISM_SCHEMA: "ukrainian-llm-eval.request-budget-receipt.v2",
                USAGE_BOUND_MECHANISM_SCHEMA: "ukrainian-llm-eval.request-budget-receipt.v3",
            }[mechanism_schema],
            "status": status,
            "mechanism_sha256": digest(self.mechanism),
            "rounds_committed": self.rounds,
            "input_tokens_committed": self.input_tokens,
            "output_tokens_reserved": self.output_tokens_reserved,
            "input_tokens_observed": self.observed_input_tokens,
            "output_tokens_including_reasoning_observed": self.observed_output_tokens,
            "tool_calls_observed": self.tool_calls,
            "formula_charge_micro_usd": self.formula_charge,
            "provider_reported_charge_micro_usd": self.provider_reported_charge,
            "reported_cost_kind": self.mechanism["usage"]["reported_cost_kind"],
        }
        if provider_bound:
            result["shared_settlement_sha256"] = (
                None if shared_settlement is None else shared_settlement["reservation_sha256"]
            )
        if usage_bound:
            ordered_final_usage = copy.deepcopy(self.ordered_final_usage)
            result.update(
                settlement_kind="conservative_final_usage_upper_bound",
                ordered_final_usage=ordered_final_usage,
                ordered_final_usage_sha256=digest(ordered_final_usage),
                rounding_scope=self.mechanism["usage_upper_bound"]["rounding_scope"],
                rounding_quantum_micro_usd=(
                    self.mechanism["usage_upper_bound"]["rounding_quantum_micro_usd"]
                ),
                maximum_additional_fees_micro_usd=(
                    self.mechanism["usage_upper_bound"]["maximum_additional_fees_micro_usd"]
                ),
                pricing_evidence_sha256=self.mechanism["pricing_evidence_sha256"],
                backend_identity_evidence_sha256=self.mechanism["backend_identity_evidence_sha256"],
                usage_semantics_evidence_sha256=(
                    self.mechanism["usage_upper_bound"]["usage_semantics_evidence_sha256"]
                ),
                additional_fees_evidence_sha256=(
                    self.mechanism["usage_upper_bound"]["additional_fees_evidence_sha256"]
                ),
                charge_upper_bound_micro_usd=(
                    None if shared_settlement is None else shared_settlement["settled_micro_usd"]
                ),
            )
        receipt = self.attempt.finalize(result, status=status)
        self._closed = True
        return receipt


class RequestBudgetController:
    """Strict route-spec registry and non-recyclable credit commitment ledger."""

    def __init__(self, route_specs: Mapping[str, Any], *, shared_ledger_path: Path | None = None):
        self._raw_route_specs = copy.deepcopy(dict(route_specs))
        self.route_specs: dict[str, Any] = {}
        self._routes: dict[str, Any] = {}
        self._store: EvidenceStore | None = None
        self._shared_ledger_path = None if shared_ledger_path is None else Path(shared_ledger_path)
        self._shared_ledger = None
        self._spending_policy: dict[str, Any] | None = None

    def prepare(self, manifest, _plan) -> None:
        routes = {route["route_id"]: route for route in manifest["routes"]}
        required = {
            route_id for route_id, route in routes.items()
            if route["request_budget_mechanism_sha256"] is not None
        }
        if set(self._raw_route_specs) != required:
            raise ExamError("request-budget integrations do not match frozen route mechanisms")
        normalized: dict[str, Any] = {}
        for route_id in sorted(required):
            value = self._raw_route_specs[route_id]
            if not isinstance(value, Mapping):
                raise ExamError("invalid request-budget route specification")
            if value.get("schema") == ROUTE_SPEC_SCHEMA:
                expected_fields = {"schema", "mechanism", "counter_command"}
            elif value.get("schema") in {
                PROVIDER_BOUND_ROUTE_SPEC_SCHEMA, USAGE_BOUND_ROUTE_SPEC_SCHEMA,
            }:
                expected_fields = {"schema", "mechanism"}
            else:
                raise ExamError("unsupported request-budget route specification")
            if set(value) != expected_fields:
                raise ExamError("invalid request-budget route specification")
            try:
                mechanism = validate_mechanism(value["mechanism"])
            except RequestBudgetError as exc:
                raise ExamError(str(exc)) from exc
            route = routes[route_id]
            reported_kind = mechanism["usage"]["reported_cost_kind"]
            if route["billing"]["kind"] in {"metered", "existing_credit"}:
                if reported_kind not in {None, "account_charge"}:
                    raise ExamError("paid route has non-charge provider cost semantics")
            elif reported_kind not in {None, "nonincremental_estimate"}:
                raise ExamError("subscription route has incremental provider cost semantics")
            if mechanism["route_sha256"] != route["route_sha256"]:
                raise ExamError("request-budget route identity drift")
            if digest(mechanism) != route["request_budget_mechanism_sha256"]:
                raise ExamError("request-budget mechanism drift")
            if mechanism["schema"] in {
                PROVIDER_BOUND_MECHANISM_SCHEMA, USAGE_BOUND_MECHANISM_SCHEMA,
            }:
                expected_spec_schema = (
                    USAGE_BOUND_ROUTE_SPEC_SCHEMA
                    if mechanism["schema"] == USAGE_BOUND_MECHANISM_SCHEMA
                    else PROVIDER_BOUND_ROUTE_SPEC_SCHEMA
                )
                if value["schema"] != expected_spec_schema:
                    raise ExamError("provider-bound mechanism requires its matching versioned route specification")
                bounds = mechanism["input_bound"]
                output = mechanism["output_parameter"]
                maximum_requests = max(
                    suite["limits"]["max_tool_calls"] + 1 for suite in manifest["suites"]
                )
                if bounds["max_requests_per_segment"] < maximum_requests:
                    raise ExamError("provider input bound omits permitted segment requests")
                if route["billing"]["max_total_input_tokens"] < (
                    bounds["max_input_tokens_per_request"] * bounds["max_requests_per_segment"]
                ):
                    raise ExamError("route input billing bound is below documented provider-context commitment")
                if any(
                    suite["limits"]["max_output_tokens"] != output["max_tokens_per_request"]
                    for suite in manifest["suites"]
                ):
                    raise ExamError("provider output bound differs from frozen suite output parameter")
                if mechanism["pricing_evidence_sha256"] != route["pricing_evidence_sha256"]:
                    raise ExamError("provider-bound pricing evidence drift")
                if (
                    mechanism["schema"] == USAGE_BOUND_MECHANISM_SCHEMA
                    and _maximum_usage_bound_charge(mechanism, route["billing"])
                    > _maximum_segment_charge(route["billing"])
                ):
                    raise ExamError("usage-derived charge upper bound exceeds frozen segment reservation")
                normalized[route_id] = {"mechanism": mechanism, "counter_command": None}
            else:
                if value["schema"] != ROUTE_SPEC_SCHEMA:
                    raise ExamError("exact-counter mechanism requires its v1 route specification")
                counter_spec = validate_command_spec(value["counter_command"])
                if counter_spec["env_names"]:
                    raise ExamError("request-budget counter must not receive environment values")
                if command_identity_sha256(counter_spec) != mechanism["counter_command_sha256"]:
                    raise ExamError("request-budget counter command drift")
                normalized[route_id] = {"mechanism": mechanism, "counter_command": counter_spec}
        self.route_specs = normalized
        self._routes = routes
        policy = manifest.get("spending_policy")
        paid_route_ids = {
            route_id
            for route_id, route in routes.items()
            if route["billing"]["kind"] in {"metered", "existing_credit"}
        }
        if policy is not None:
            legacy_paid_route_ids = sorted(
                route_id
                for route_id in paid_route_ids
                if (
                    route_id not in normalized
                    or normalized[route_id]["mechanism"]["schema"]
                    not in {PROVIDER_BOUND_MECHANISM_SCHEMA, USAGE_BOUND_MECHANISM_SCHEMA}
                )
            )
            if legacy_paid_route_ids:
                raise ExamError(
                    "sequential shared spending policy requires provider-bound request budgets "
                    "for every paid route"
                )
        has_provider_bound_paid = any(
            item["mechanism"]["schema"] in {
                PROVIDER_BOUND_MECHANISM_SCHEMA, USAGE_BOUND_MECHANISM_SCHEMA,
            }
            and route_id in paid_route_ids
            for route_id, item in normalized.items()
        )
        has_usage_bound_paid = any(
            item["mechanism"]["schema"] == USAGE_BOUND_MECHANISM_SCHEMA
            and route_id in paid_route_ids
            for route_id, item in normalized.items()
        )
        if has_provider_bound_paid:
            if not isinstance(policy, Mapping) or policy.get("mode") != "sequential_shared_cap":
                raise ExamError("provider-bound paid request budgets require sequential shared spending policy")
            allowed_policies = [
                (
                    "ukrainian-llm-eval.spending-policy.v2",
                    "authoritative_account_charge_or_conservative_final_usage_upper_bound",
                ),
            ]
            if not has_usage_bound_paid:
                # The broader policy also authorizes authoritative charges.
                # Settlement evidence still follows each mechanism's version.
                allowed_policies.append((
                    "ukrainian-llm-eval.spending-policy.v1",
                    "authoritative_final_account_charge_only",
                ))
            if (policy.get("schema"), policy.get("settlement")) not in allowed_policies:
                raise ExamError("request-budget mechanism and spending settlement policy differ")
            if self._shared_ledger_path is None:
                raise ExamError("sequential shared spending policy requires a runtime ledger path")
            self._spending_policy = copy.deepcopy(dict(policy))
        elif policy is not None:
            raise ExamError("sequential shared spending policy has no provider-bound paid route")

        # The execution-plan constructor reserves new spend with the same
        # whole-segment billing formula used below by the shared ledger.  Check
        # that contract before admission can retain a successful probe receipt.
        # Existing-credit routes intentionally have a zero *new-spend*
        # reservation in the plan; their full charge is separately retained as
        # a credit commitment and must not be compared to this field.
        for route_id in normalized:
            route = routes[route_id]
            if route["billing"]["kind"] != "metered":
                continue
            expected_reservation = _maximum_segment_charge(route["billing"])
            for cell in _plan.get("cells", []):
                if cell.get("route_id") != route_id:
                    continue
                for segment in cell.get("segments", []):
                    if segment.get("reserved_micro_usd") != expected_reservation:
                        raise ExamError("execution-plan reservation differs from request-budget maximum charge")

    def bind(self, root: Path) -> None:
        self.validate_execution_root(root)
        if self._spending_policy is not None and self._shared_ledger is None:
            from .spending_ledger import SharedSpendingLedger

            self._shared_ledger = SharedSpendingLedger(
                self._shared_ledger_path,
                ledger_id=self._spending_policy["ledger_id"],
                cap_micro_usd=self._spending_policy["authorized_cap_micro_usd"],
            )
        self._store = EvidenceStore(root / "request-budget-evidence")
        # The enclosing scheduler lock proves no cooperating executor can still
        # own these attempts. Preserve the commitment and mark the abandoned
        # controller lifecycle without making it reusable.
        for budget_id, evidence in self._store.verify_all().items():
            if not evidence["complete"]:
                metadata = evidence.get("metadata")
                commitment = metadata.get("credit_commitment_micro_usd") if isinstance(metadata, Mapping) else None
                if type(commitment) is not int or commitment < 0:
                    raise ExamError("retained request-budget metadata is invalid")
                self._store.finalize(
                    budget_id,
                    {
                        "schema": "ukrainian-llm-eval.request-budget-interruption.v1",
                        "status": "interrupted",
                        "credit_commitment_micro_usd": commitment,
                    },
                    status="interrupted",
                )

    def validate_execution_root(self, root: Path) -> None:
        """Reject a per-run ledger path before the scheduler creates outputs."""

        if self._spending_policy is None:
            return
        root_resolved = root.resolve()
        ledger_resolved = self._shared_ledger_path.resolve()
        if ledger_resolved == root_resolved or ledger_resolved.is_relative_to(root_resolved):
            raise ExamError("shared spending ledger must be outside the execution root")

    def remaining_ceiling_micro_usd(self) -> int:
        if self._shared_ledger is None:
            raise ExamError("request-budget controller has no shared spending ledger")
        return self._shared_ledger.snapshot()["remaining_new_spend_micro_usd"]

    @property
    def uses_sequential_shared_cap(self) -> bool:
        return self._spending_policy is not None

    def for_attempt(
        self, route, config, attempt_id: str, admission_receipt: Mapping[str, Any], *,
        reservation_id: str | None = None, reservation_binding: Mapping[str, Any] | None = None,
    ) -> RequestBudget | None:
        kind = route["billing"]["kind"]
        if route["request_budget_mechanism_sha256"] is None:
            return None
        if self._store is None:
            raise ExamError("request-budget controller is not bound to the execution root")
        if config.get("adapter") not in {"chat-http", "responses-http", "opencode"}:
            raise ExamError("request-level budget requires an exact-byte HTTP adapter")
        if config.get("provider") != self.route_specs[route["route_id"]]["mechanism"]["provider"]:
            raise ExamError("request-budget provider identity drift")
        maximum_charge = _maximum_segment_charge(route["billing"])
        credit = admission_receipt.get("credit_available_micro_usd")
        account_sha256 = admission_receipt.get("account_sha256")
        if not isinstance(account_sha256, str) or _SHA256_RE.fullmatch(account_sha256) is None:
            raise ExamError("request-budget admission lacks account identity")
        existing = self._store.verify_all()
        budget_id = request_budget_attempt_id(attempt_id)
        if budget_id in existing:
            raise ExamError("request-budget commitment already exists for candidate attempt")
        committed_credit = 0
        for item in existing.values():
            metadata = item.get("metadata")
            if not isinstance(metadata, Mapping):
                raise ExamError("retained request-budget metadata is invalid")
            retained_account = metadata.get("account_sha256")
            commitment = metadata.get("credit_commitment_micro_usd")
            if (
                not isinstance(retained_account, str)
                or _SHA256_RE.fullmatch(retained_account) is None
                or type(commitment) is not int
                or commitment < 0
            ):
                raise ExamError("retained request-budget metadata is invalid")
            if retained_account == account_sha256:
                committed_credit += commitment
        if kind == "existing_credit":
            if self._shared_ledger is None and (
                type(credit) is not int or credit < 0 or committed_credit + maximum_charge > credit
            ):
                raise ExamError("existing-credit balance cannot cover retained request commitments")
        elif credit is not None:
            raise ExamError("metered request budget must not claim a credit balance")
        shared_reservation = None
        mechanism = self.route_specs[route["route_id"]]["mechanism"]
        if mechanism["schema"] in {
            PROVIDER_BOUND_MECHANISM_SCHEMA, USAGE_BOUND_MECHANISM_SCHEMA,
        }:
            if self._shared_ledger is None or reservation_id is None or reservation_binding is None:
                raise ExamError("provider-bound request budget lacks shared reservation inputs")
            from .spending_ledger import SpendingCapExceeded, SpendingLedgerError

            try:
                shared_reservation = self._shared_ledger.reserve(
                    reservation_id,
                    reservation_binding,
                    maximum_micro_usd=maximum_charge,
                    funding_kind=kind,
                    account_sha256=account_sha256,
                    credit_available_micro_usd=credit,
                )
            except SpendingCapExceeded:
                raise
            except SpendingLedgerError as exc:
                raise ExamError(str(exc)) from exc
            if shared_reservation["replayed"]:
                raise ExamError("shared spending reservation already exists for candidate attempt")
        metadata = {
                "denominator": 0,
                "candidate_attempt_id": attempt_id,
                "route_sha256": route["route_sha256"],
                "account_sha256": account_sha256,
                "mechanism_sha256": route["request_budget_mechanism_sha256"],
                "billing_kind": kind,
                "credit_commitment_micro_usd": maximum_charge if kind == "existing_credit" else 0,
                "maximum_segment_charge_micro_usd": maximum_charge,
            }
        if shared_reservation is not None:
            metadata["shared_reservation_sha256"] = shared_reservation["reservation_sha256"]
        if mechanism["schema"] == USAGE_BOUND_MECHANISM_SCHEMA:
            metadata["request_budget_mechanism_schema"] = USAGE_BOUND_MECHANISM_SCHEMA
        attempt = self._store.start(
            metadata,
            attempt_id=budget_id,
        )
        spec = self.route_specs[route["route_id"]]
        return RequestBudget(
            route,
            config,
            spec["mechanism"],
            spec["counter_command"],
            attempt,
            maximum_charge=maximum_charge,
            shared_ledger=self._shared_ledger if shared_reservation is not None else None,
            shared_reservation_id=reservation_id,
        )


__all__ = [
    "CACHE_BILLING",
    "COUNTER_RESULT_SCHEMA",
    "INPUT_SEMANTICS",
    "MECHANISM_SCHEMA",
    "PROVIDER_BOUND_MECHANISM_SCHEMA",
    "PROVIDER_BOUND_ROUTE_SPEC_SCHEMA",
    "ROUTE_SPEC_SCHEMA",
    "SERIALIZER",
    "USAGE_BOUND_MECHANISM_SCHEMA",
    "USAGE_BOUND_ROUTE_SPEC_SCHEMA",
    "RequestBudget",
    "RequestBudgetController",
    "RequestBudgetError",
    "mechanism_sha256",
    "request_budget_attempt_id",
    "validate_mechanism",
    "verify_request_budget_evidence",
]
