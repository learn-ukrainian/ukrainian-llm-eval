"""Read-only OpenRouter admission for selected personal Gemma routes.

The authenticated creator user is not an organization billing pool. Provider
funds support paid-route eligibility; the zero-priced Google route still checks
key identity and validity. The independent shared ledger bounds new spend. This collector neither reserves funds nor sends candidate requests.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

from probe_common import (
    MAX_BYTES,
    NoRedirect,
    ProbeError,
    canonical,
    digest,
    fail,
    file_hash,
    integer,
    parse,
    text,
    timestamp,
    utcnow,
    verified_runtime,
)

MODEL = "google/gemma-4-31b-it"
FREE_MODEL = MODEL + ":free"
FREE_BACKEND = "google-ai-studio"
FREE_PROVIDER = "Google AI Studio"
BACKEND = "venice/bf16"
BACKEND_PROVIDERS = ((BACKEND, "Venice"), ("novita/bf16", "Novita"))
KEY_URL = "https://openrouter.ai/api/v1/key"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
MODEL_URL = "https://openrouter.ai/api/v1/models/google/gemma-4-31b-it/endpoints"
FREE_MODEL_URL = "https://openrouter.ai/api/v1/models/google/gemma-4-31b-it:free/endpoints"


def free_route(config):
    return (config["provider"], config["model"], config["backend"], config["expected_provider_name"]) == (
        "openrouter", FREE_MODEL, FREE_BACKEND, FREE_PROVIDER)


def check_route(config):
    if not free_route(config) and not (config["provider"] == "openrouter"
            and config["model"] == MODEL
            and (config["backend"], config["expected_provider_name"]) in BACKEND_PROVIDERS):
        fail("unsupported_route")
    if free_route(config) and (config["maximum_segment_micro_usd"] != 0 or any(
            config["pricing"][name] != 0 for name in ("input_micro_usd_per_million_tokens",
                "output_micro_usd_per_million_tokens", "tool_round_micro_usd"))):
        fail("free_route_price_policy_invalid")


def sha(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        fail("invalid_digest")
    return value


def validate_request(request):
    # Subscription helpers intentionally reject null effort; Gemma requires it.
    fields = {"schema", "nonce", "requested_at", "route_sha256", "model", "effort", "condition",
              "composite_sha256", "requirements", "request_sha256"}
    if not isinstance(request, dict) or set(request) != fields:
        fail("invalid_request")
    if request["schema"] != "ukrainian-llm-eval.admission-request.v1":
        fail("invalid_request")
    if not isinstance(request["nonce"], str) or re.fullmatch(r"[0-9a-f]{32}", request["nonce"]) is None:
        fail("invalid_nonce")
    if request["request_sha256"] != digest({k: v for k, v in request.items() if k != "request_sha256"}):
        fail("request_hash_mismatch")
    for key in ("route_sha256", "composite_sha256"):
        sha(request[key])
    age = (datetime.now(UTC) - timestamp(request["requested_at"])).total_seconds()
    if not 0 <= age <= 300:
        fail("stale_request")
    if request["model"] not in {MODEL, FREE_MODEL} or request["effort"] is not None:
        fail("request_route_mismatch")
    if request["condition"] not in {"closed_book", "sources"}:
        fail("condition_unsupported")
    requirements = request["requirements"]
    keys = {"input_utf8_bytes", "max_total_input_tokens", "max_total_output_tokens", "max_output_tokens",
            "max_tool_calls", "timeout_seconds", "tool_policy_sha256"}
    if not isinstance(requirements, dict) or set(requirements) != keys:
        fail("invalid_requirements")
    for key in keys - {"tool_policy_sha256"}:
        integer(requirements[key])
    sha(requirements["tool_policy_sha256"])
    return request


def provider_json(url, token=None):
    """Exact GET allowlist, no proxies/redirects/refresh, bounded decimal JSON."""
    if url not in {KEY_URL, CREDITS_URL, MODEL_URL, FREE_MODEL_URL}:
        fail("provider_endpoint_rejected")
    if (url in {MODEL_URL, FREE_MODEL_URL}) != (token is None):
        fail("provider_credential_scope_rejected")
    headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
    if token is not None:
        headers["Authorization"] = "Bearer " + text(token)
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200 or response.geturl() != url:
                fail("provider_response_rejected")
            if response.headers.get("Age") not in (None, "0"):
                fail("cached_provider_response")
            raw = response.read(MAX_BYTES + 1)
            parse(raw)  # Shared strict bounds, duplicate-key and non-finite checks.
            value = json.loads(raw, parse_float=Decimal)
            if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
                fail("provider_response_invalid")
            return value["data"]
    except urllib.error.HTTPError as exc:
        if type(exc.code) is int and 100 <= exc.code <= 599:
            fail("provider_http_" + str(exc.code))
        fail("provider_status_unavailable")
    except (urllib.error.URLError, OSError):
        fail("provider_status_unavailable")


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        fail("provider_amount_invalid")
    try:
        result = Decimal(value)
    except InvalidOperation:
        fail("provider_amount_invalid")
    if not result.is_finite() or result < 0 or result > Decimal("1e15") or result.as_tuple().exponent < -24:
        fail("provider_amount_invalid")
    return result


def micro_usd(value):
    # Never round spendable funds upward.
    return int((amount(value) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))


def local_dependency(base, declaration):
    if not isinstance(declaration, dict) or set(declaration) != {"name", "byte_sha256"}:
        fail("dependency_invalid")
    name = declaration["name"]
    if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
        fail("dependency_path_rejected")
    path = base / name
    if file_hash(path) != sha(declaration["byte_sha256"]):
        fail("dependency_identity_mismatch")
    return path


def support_for(config, base):
    check_route(config)
    support = config["support"]
    if (config["effort"] is not None
            or type(config["reasoning_enabled"]) is not bool
            or config["account_scope"] != "personal_provider_user"):
        fail("unsupported_route")
    if (support["runtime_files_sha256"] != digest(config["runtime_files"])
            or support["pricing_sha256"] != digest(config["pricing"])
            or support["capability_sha256"] != digest(config["capability"])):
        fail("support_identity_mismatch")
    for field in ("provider", "model", "backend", "reasoning_enabled", "account_scope", "credential_sha256"):
        if support[field] != config[field]:
            fail("support_route_mismatch")
    sha(config["credential_sha256"])
    if not support["artifacts"]:
        fail("support_evidence_missing")
    for artifact in support["artifacts"]:
        local_dependency(base, artifact)
    for claim in ("personal_key_ownership_verified", "same_credential_native_execution_verified",
                  "native_control_enforcement_verified", "provider_routing_and_price_caps_verified",
                  "byte_token_upper_bound_verified", "all_non_token_fees_excluded",
                  "initial_history_verified", "output_headroom_verified"):
        if support.get(claim) is not True:
            fail("support_proof_unavailable")
    initial_capacity(config["capability"], support)
    local_dependency(base, config["budget_wheel"])
    return support


def initial_capacity(capability, support):
    """Validate reviewed initial history and combined-window/output semantics."""
    if "permitted_history_tokens" in support:
        fail("legacy_history_bound_rejected")
    if (support.get("initial_history_verified") is not True
            or support.get("output_headroom_verified") is not True):
        fail("support_proof_unavailable")
    history = integer(support.get("initial_history_tokens"))
    framing = integer(support.get("framing_tokens"), 1)
    window = integer(support.get("context_window_tokens"), 1)
    headroom = integer(support.get("output_headroom_tokens"), 1)
    available_input = integer(capability["context_input_tokens"], 1)
    # Headroom is source-reviewed maximum runtime output, not a request cap.
    # context_input_tokens is already net; never subtract headroom from it.
    if (headroom < integer(capability["max_output_tokens"], 1)
            or headroom >= window or available_input > window - headroom):
        fail("output_headroom_invalid")
    return framing, history, available_input


def requirements_for(request, config, support):
    validate_request(request)
    check_route(config)
    if request["model"] != config["model"]:
        fail("request_route_mismatch")
    if request["condition"] not in support["conditions"]:
        fail("condition_unsupported")
    pricing, entitlement, capability = (config[name] for name in ("pricing", "entitlement", "capability"))
    fields = (
        {"route_sha256", "currency", "input_micro_usd_per_million_tokens", "output_micro_usd_per_million_tokens",
         "tool_round_micro_usd"},
        {"route_sha256", "account_sha256", "billing_kind", "zero_incremental", "valid_until"},
        {"route_sha256", "model", "effort", "context_input_tokens", "max_output_tokens", "max_tool_calls",
         "timeout_seconds", "tool_policy_sha256"},
    )
    for state, expected in zip((pricing, entitlement, capability), fields, strict=True):
        if not isinstance(state, dict) or set(state) != expected:
            fail("invalid_frozen_state")
        if state["route_sha256"] != request["route_sha256"]:
            fail("state_route_mismatch")
    sha(entitlement["account_sha256"])
    if (entitlement["billing_kind"] != "metered" or entitlement["zero_incremental"] is not False
            or timestamp(entitlement["valid_until"]) <= datetime.now(UTC)):
        fail("entitlement_state_invalid")
    if capability["model"] != config["model"] or capability["effort"] is not None:
        fail("capability_route_mismatch")
    requirements = request["requirements"]
    if capability["tool_policy_sha256"] != requirements["tool_policy_sha256"]:
        fail("tool_policy_mismatch")
    for field in ("max_output_tokens", "max_tool_calls", "timeout_seconds"):
        if integer(capability[field], 1) < requirements[field]:
            fail("capacity_insufficient")
    framing, history, available_input = initial_capacity(capability, support)
    required_input = requirements["input_utf8_bytes"] + framing + history
    if required_input > min(available_input, requirements["max_total_input_tokens"]):
        fail("input_does_not_fit")
    if pricing["currency"] != "USD" or pricing["tool_round_micro_usd"] != 0:
        fail("pricing_unverified")
    cost = sum((integer(pricing[rate]) * requirements[tokens] + 999_999) // 1_000_000
               for rate, tokens in (("input_micro_usd_per_million_tokens", "max_total_input_tokens"),
                                    ("output_micro_usd_per_million_tokens", "max_total_output_tokens")))
    maximum = integer(config["maximum_segment_micro_usd"], 0 if free_route(config) else 1)
    if cost > maximum:
        fail("segment_cost_exceeds_frozen_maximum")
    return maximum, required_input


def read_commitments(config, base):
    """Import only the declared snapshot wheel; never use ambient installation."""
    wheel = local_dependency(base, config["budget_wheel"])
    if wheel.suffix != ".whl" or any(
        name == "ukrainian_llm_eval" or name.startswith("ukrainian_llm_eval.") for name in sys.modules
    ):
        fail("budget_runtime_not_isolated")
    sys.path.insert(0, str(wheel))
    try:
        ledger_module = importlib.import_module("ukrainian_llm_eval.spending_ledger")
        if ledger_module.__file__ != str(wheel) + "/ukrainian_llm_eval/spending_ledger.py":
            fail("budget_runtime_identity_mismatch")
        ledger = config["ledger"]
        snapshot = ledger_module.SharedSpendingLedger.inspect_readiness(
            Path(ledger["path"]), ledger_id=ledger["ledger_id"], cap_micro_usd=ledger["cap_micro_usd"],
        )
        local_dependency(base, config["budget_wheel"])
        return snapshot
    except ProbeError:
        raise
    except Exception:  # noqa: BLE001 -- the ledger error may contain a private path
        fail("existing_budget_unreadable")
    finally:
        sys.path.remove(str(wheel))


def collect(config):
    check_route(config)
    free = free_route(config)
    env_name = config["key_env"]
    if not isinstance(env_name, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env_name) is None:
        fail("credential_input_invalid")
    token = text(os.environ.get(env_name))
    if hashlib.sha256(token.encode()).hexdigest() != config["credential_sha256"]:
        fail("credential_identity_mismatch")
    key = provider_json(KEY_URL, token)
    funds = None if free else provider_json(CREDITS_URL, token)
    models = provider_json(FREE_MODEL_URL if free else MODEL_URL)
    account = digest({"provider": "openrouter", "creator_user_id": text(key.get("creator_user_id"))})
    if account != config["entitlement"]["account_sha256"]:
        fail("provider_user_identity_mismatch")
    if (type(key.get("is_free_tier")) is not bool or (not free and key["is_free_tier"])
            or key.get("is_management_key") is not False):
        fail("metered_key_status_unverified")
    if key.get("is_provisioning_key") is not False:
        fail("metered_key_status_unverified")
    expiry = timestamp(key.get("expires_at"))
    if expiry <= datetime.now(UTC) or timestamp(config["entitlement"]["valid_until"]) > expiry:
        fail("key_expiry_invalid")
    # Round the difference, not each component: flooring usage could invent a
    # spendable micro-dollar when fractional charges straddle the boundary.
    # Zero is a spend bound for the free route, not a claim about account funds.
    available = 0 if free else int(((amount(funds.get("total_credits")) - amount(funds.get("total_usage")))
                     * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
    if "limit" not in key or "limit_remaining" not in key:
        fail("key_limit_unknown")
    if key["limit"] is not None:
        limit = amount(key["limit"])
        remaining = amount(key.get("limit_remaining"))
        if remaining > limit:
            fail("key_limit_invalid")
        available = min(available, micro_usd(remaining))
    elif key.get("limit_remaining") is not None:
        fail("key_limit_invalid")
    if models.get("id") != config["model"]:
        fail("provider_model_identity_mismatch")
    endpoints = models.get("endpoints")
    if not isinstance(endpoints, list):
        fail("provider_capacity_unknown")
    selected = [row for row in endpoints if isinstance(row, dict) and row.get("tag") == config["backend"]]
    if len(selected) != 1:
        fail("provider_backend_unavailable")
    endpoint = selected[0]
    if endpoint.get("model_id") != config["model"] or endpoint.get("provider_name") != config["expected_provider_name"]:
        fail("provider_backend_identity_mismatch")
    if not free and endpoint.get("quantization") != "bf16":
        fail("provider_precision_unverified")
    if type(endpoint.get("status")) is not int or endpoint["status"] != 0:
        fail("provider_backend_unhealthy")
    if (endpoint.get("context_length") != integer(config["support"].get("context_window_tokens"), 1)
            or endpoint.get("max_completion_tokens") != config["capability"]["max_output_tokens"]):
        fail("provider_capacity_drift")
    prompt_maximum = endpoint.get("max_prompt_tokens")
    if prompt_maximum is not None and integer(prompt_maximum, 1) < config["capability"]["context_input_tokens"]:
        fail("provider_input_capacity_requires_review")
    parameters = endpoint.get("supported_parameters")
    required_parameters = {"tools", "tool_choice", "reasoning", "max_tokens"} if free else {
        "tools", "structured_outputs", "reasoning", "max_tokens"}
    if not isinstance(parameters, list) or not required_parameters <= set(parameters):
        fail("provider_capability_unavailable")
    prices = endpoint.get("pricing")
    if not isinstance(prices, dict) or set(prices) - {"prompt", "completion", "input_cache_read", "discount", "request"}:
        fail("provider_fees_unknown")
    for remote, local in (("prompt", "input_micro_usd_per_million_tokens"),
                          ("completion", "output_micro_usd_per_million_tokens")):
        if amount(prices.get(remote)) * 1_000_000_000_000 != config["pricing"][local]:
            fail("provider_pricing_drift")
    if "request" in prices and amount(prices["request"]) != 0:
        fail("provider_fees_unknown")
    if "discount" in prices and amount(prices["discount"]) != 0:
        fail("provider_pricing_drift")
    if "input_cache_read" in prices and amount(prices["input_cache_read"]) > amount(prices["prompt"]):
        fail("provider_pricing_drift")
    return {"account_sha256": account, "available_micro_usd": available, "observed_at": utcnow()}


def build_result(request, config, support, observation, snapshot):
    maximum, required_input = requirements_for(request, config, support)
    observed_at = utcnow()
    if not timestamp(request["requested_at"]) <= timestamp(observation["observed_at"]) <= timestamp(observed_at):
        fail("status_not_fresh")
    if observation["account_sha256"] != config["entitlement"]["account_sha256"]:
        fail("provider_user_identity_mismatch")
    unresolved = integer(snapshot["unresolved_new_spend_micro_usd"])
    if ((not free_route(config) and observation["available_micro_usd"] < maximum + unresolved)
            or integer(snapshot["remaining_new_spend_micro_usd"]) < maximum):
        fail("next_reservation_unfunded")

    def record(name, observed):
        state = config[name]
        return {"state": state, "state_sha256": digest(state), "observed": observed}

    return {"schema": "ukrainian-llm-eval.admission-result.v1", "nonce": request["nonce"],
            "request_sha256": request["request_sha256"], "observed_at": observed_at,
            "pricing": record("pricing", {"conservative_segment_cost_micro_usd": maximum,
                                           "incremental_segment_cost_micro_usd": maximum}),
            "entitlement": record("entitlement", {"eligible": True, "credit_available_micro_usd": None}),
            "capability": record("capability", {"healthy": True, "input_fits": True,
                                                "required_input_tokens": required_input})}


def main():
    try:
        if len(sys.argv) != 2:
            fail("invalid_arguments")
        path = Path(sys.argv[1])
        config = parse(path.read_bytes())
        runtime = verified_runtime(config)
        request = validate_request(parse(sys.stdin.buffer.read(MAX_BYTES + 1)))
        support = support_for(config, path.parent)
        requirements_for(request, config, support)  # Fail before credential use.
        observation = collect(config)
        snapshot = read_commitments(config, path.parent)
        result = build_result(request, config, support, observation, snapshot)
        if verified_runtime(config) != runtime:
            fail("runtime_identity_mismatch")
        sys.stdout.buffer.write(canonical(result) + b"\n")
        return 0
    except ProbeError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:  # noqa: BLE001 -- no raw provider/account/credential errors
        print("probe_failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
