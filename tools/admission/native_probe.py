"""Trusted V2 subscription probe. Invoke with a declared frozen input JSON.

No benchmark text or credentials belong in this file or its input JSON. The
only credential input is existing native provisioning / an explicitly passed
ADMISSION_BEARER_TOKEN environment value. Never discovers or refreshes tokens.
"""
from __future__ import annotations

import sys
from pathlib import Path

import codex_status
import subscription_status
from probe_common import (
    MAX_BYTES,
    ProbeError,
    canonical,
    digest,
    fail,
    file_hash,
    integer,
    parse,
    timestamp,
    utcnow,
    validate_request,
    verified_runtime,
)


def collect(config, requested_at=None):
    provider = config["provider"]
    if provider == "codex":
        values = codex_status.collect(config)
        identity = codex_status.normalize(*values[:-1], config["model"], config["effort"], config["quota_key"])
    elif provider == "claude":
        values = subscription_status.collect_claude(config)
        identity = subscription_status.normalize_claude(*values[:-1], config["model"])
    elif provider == "agy":
        values = subscription_status.collect_agy(config, requested_at=requested_at)
        identity = subscription_status.normalize_agy(*values[:-1], config["model"], config["effort"])
    else:
        fail("provider_unsupported")
    return {"account_sha256": identity, "status_observed_at": values[-1]}


def inspect_codex(config, diagnostics=None):
    """Safe diagnostic only: explicitly not a validated admission response."""
    account, usage, models, observed = codex_status.collect(config, diagnostics)
    return {"admission": False, "observed_at": observed,
            "account_type": (account.get("account") or {}).get("type"),
            "plan_type": (account.get("account") or {}).get("planType"),
            "account_id_present": isinstance(usage.get("accountId"), str) and bool(usage["accountId"]),
            "ordinary_usage_allowed": usage.get("ordinaryUsageAllowed"),
            "quota": [{"limit_id": key, "limit_name": value.get("limitName"),
                       "normal_model_slug": value.get("normalModelSlug"), "plan_type": value.get("planType"),
                       "used_percent": [(value.get(window) or {}).get("usedPercent")
                                        for window in ("primary", "secondary")],
                       "has_credits": (value.get("credits") or {}).get("hasCredits"),
                       "unlimited_credits": (value.get("credits") or {}).get("unlimited")}
                      for key, value in (usage.get("rateLimitsByLimitId") or {}).items()],
            "selected_model": [{"model": item.get("model"),
                                "efforts": [row.get("reasoningEffort")
                                            for row in item.get("supportedReasoningEfforts", [])]}
                               for item in models.get("data", []) if item.get("model") == config["model"]]}


def inspect_subscription(config, diagnostics=None):
    """Diagnostic shape/normalized status only, never native admission proof."""
    if config["provider"] == "claude":
        auth, profile, usage, observed = subscription_status.collect_claude(config, diagnostics)
        account, org = profile.get("account") or {}, profile.get("organization") or {}
        return {"admission": False, "observed_at": observed,
            "logged_in": auth.get("loggedIn") is True, "native_subscription_auth": auth.get("authMethod") == "claude.ai",
            "known_paid_plan": auth.get("subscriptionType") in {"pro", "max", "team"},
            "account_uuid_present": bool(account.get("uuid")), "org_uuid_present": bool(org.get("uuid")),
            "same_org": bool(org.get("uuid")) and auth.get("orgId") == org["uuid"],
            "same_email": bool(account.get("email")) and auth.get("email") == account["email"],
            "extra_usage_explicitly_disabled": (usage.get("extra_usage") or {}).get("is_enabled") is False,
            "quota_windows_present": all(isinstance(usage.get(key), dict) for key in ("five_hour", "seven_day"))}
    if config["provider"] == "agy":
        identity, status, quota, observed = subscription_status.collect_agy(config, diagnostics)
        account = subscription_status.normalize_agy(identity, status, quota, config["model"], config["effort"])
        return {"admission": False, "observed_at": observed, "account_sha256": account,
                "owned_fresh_native_status": True, "selected_model": config["model"],
                "selected_effort": config["effort"], "selected_quota_available": True,
                "current_user_tier": status["userStatus"]["userTier"]["id"]}
    fail("provider_unsupported")


def support_for(config, base):
    """Bind reviewed support bytes to this exact runtime and frozen V2 states.

    This validates provenance linkage, not the truth of an arbitrary document.
    The accountable reviewer must approve all these declared bytes before use.
    """
    support = config["support"]
    if support["runtime_files_sha256"] != digest(config["runtime_files"]):
        fail("support_runtime_mismatch")
    artifacts = support["artifacts"]
    if not artifacts:
        fail("support_evidence_missing")
    for artifact in artifacts:
        name = artifact["name"]
        if Path(name).name != name or name in {".", ".."}:
            fail("support_path_rejected")
        if file_hash(base / name) != artifact["byte_sha256"]:
            fail("support_evidence_mismatch")
    for field in ("provider", "model", "effort"):
        if support[field] != config[field]:
            fail("support_route_mismatch")
    if config["provider"] == "codex" and (
        support.get("quota_key") != config.get("quota_key") or support.get("model_quota_mapping_verified") is not True
    ):
        fail("quota_mapping_unverified")
    if support["capability_sha256"] != digest(config["capability"]):
        fail("support_capacity_mismatch")
    if support["pricing_sha256"] != digest(config["pricing"]):
        fail("support_pricing_mismatch")
    # Required review conclusions concern actual adapter behavior and every
    # additional-charge path, not just the presence of a setting in JSON.
    for key in ("current_subscription_endpoint_verified", "all_additional_charge_paths_excluded",
                "api_credentials_excluded", "native_control_enforcement_verified",
                "capacity_source_verified", "byte_token_upper_bound_verified",
                "initial_history_verified"):
        if support.get(key) is not True:
            fail("support_proof_unavailable")
    initial_capacity(config["capability"], support)
    return support


def initial_capacity(capability, support):
    """Validate reviewed initial history and discriminated native input semantics."""
    if "permitted_history_tokens" in support:
        fail("legacy_history_bound_rejected")
    if support.get("initial_history_verified") is not True:
        fail("support_proof_unavailable")
    history = integer(support.get("initial_history_tokens"))
    framing = integer(support.get("framing_tokens"), 1)
    window = integer(support.get("context_window_tokens"), 1)
    available_input = integer(capability["context_input_tokens"], 1)
    basis = support.get("input_capacity_basis")
    if basis == "combined_window":
        if {"effective_context_window_percent", "native_usable_input_verified"} & support.keys():
            fail("input_capacity_basis_conflict")
        if support.get("output_headroom_verified") is not True:
            fail("support_proof_unavailable")
        headroom = integer(support.get("output_headroom_tokens"), 1)
        if (headroom < integer(capability["max_output_tokens"], 1)
                or headroom >= window or available_input > window - headroom):
            fail("output_headroom_invalid")
    elif basis == "native_usable_input":
        if {"output_headroom_tokens", "output_headroom_verified"} & support.keys():
            fail("input_capacity_basis_conflict")
        if support.get("native_usable_input_verified") is not True:
            fail("support_proof_unavailable")
        percent = integer(support.get("effective_context_window_percent"), 1)
        if percent > 100 or available_input > window * percent // 100:
            fail("native_input_capacity_invalid")
        # Source-defined usable input already reserves native headroom. It is
        # not a separate numeric output guarantee; do not subtract output again.
    else:
        fail("input_capacity_basis_unknown")
    return framing, history, available_input


def build_result(request, config, observation, support):
    validate_request(request)
    for key in ("model", "effort"):
        if request[key] != config[key]:
            fail("request_route_mismatch")
    if request["condition"] not in support["conditions"]:
        fail("condition_unsupported")
    observed_at = utcnow()
    if not timestamp(request["requested_at"]) <= timestamp(observation["status_observed_at"]) <= timestamp(observed_at):
        fail("status_not_fresh")
    pricing, entitlement, capability = (dict(config[name]) for name in ("pricing", "entitlement", "capability"))
    expected = (
        {"route_sha256", "currency", "input_micro_usd_per_million_tokens", "output_micro_usd_per_million_tokens",
         "tool_round_micro_usd"},
        {"route_sha256", "account_sha256", "billing_kind", "zero_incremental", "valid_until", "verification"},
        {"route_sha256", "model", "effort", "context_input_tokens", "max_output_tokens", "max_tool_calls",
         "timeout_seconds", "tool_policy_sha256"},
    )
    for state, fields in zip((pricing, entitlement, capability), expected, strict=True):
        if set(state) != fields:
            fail("invalid_frozen_state")
        if state["route_sha256"] != request["route_sha256"]:
            fail("state_route_mismatch")
    if (entitlement["account_sha256"] != observation["account_sha256"]
            or entitlement["billing_kind"] != "verified_subscription"
            or entitlement["verification"] != "live_subscription" or entitlement["zero_incremental"] is not True):
        fail("entitlement_state_mismatch")
    # These collectors do not expose provider subscription expiry. Do not carry
    # a made-up/reset/token expiry date into the strict V2 response.
    if entitlement["valid_until"] is not None:
        fail("subscription_expiry_unverified")
    requirements = request["requirements"]
    if capability["model"] != request["model"] or capability["effort"] != request["effort"]:
        fail("capability_route_mismatch")
    if capability["tool_policy_sha256"] != requirements["tool_policy_sha256"]:
        fail("tool_policy_mismatch")
    for field in ("max_output_tokens", "max_tool_calls", "timeout_seconds"):
        if integer(capability[field]) < requirements[field]:
            fail("capacity_insufficient")
    # Initial request only: full prompt bytes plus disjoint reviewed framing
    # and actual initial history. Later tool turns retain their own failures.
    framing, history, available_input = initial_capacity(capability, support)
    required_input = requirements["input_utf8_bytes"] + framing + history
    if required_input > min(available_input, requirements["max_total_input_tokens"]):
        fail("input_does_not_fit")
    if pricing["currency"] != "USD":
        fail("pricing_unverified")
    cost = sum((integer(pricing[field]) * requirements[tokens] + 999_999) // 1_000_000
               for field, tokens in (("input_micro_usd_per_million_tokens", "max_total_input_tokens"),
                                     ("output_micro_usd_per_million_tokens", "max_total_output_tokens")))
    cost += integer(pricing["tool_round_micro_usd"]) * requirements["max_tool_calls"]

    def record(state, observed):
        return {"state": state, "state_sha256": digest(state), "observed": observed}

    return {"schema": "ukrainian-llm-eval.admission-result.v2", "nonce": request["nonce"],
            "request_sha256": request["request_sha256"], "observed_at": observed_at,
            "pricing": record(pricing, {"conservative_segment_cost_micro_usd": cost,
                                        "incremental_segment_cost_micro_usd": 0}),
            "entitlement": record(entitlement, {"eligible": True, "credit_available_micro_usd": None,
                "subscription_status": "active", "paid_fallback_enabled": False,
                "status_observed_at": observation["status_observed_at"]}),
            "capability": record(capability, {"healthy": True, "input_fits": True,
                                              "required_input_tokens": required_input})}


def main():
    diagnostics = []
    inspect = len(sys.argv) == 3 and sys.argv[2] in {"--inspect-codex", "--inspect-subscription"}
    try:
        if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and not inspect):
            fail("invalid_arguments")
        path = Path(sys.argv[1])
        config = parse(path.read_bytes())
        identity = verified_runtime(config)
        if len(sys.argv) == 3:
            if sys.argv[2] == "--inspect-codex":
                if config["provider"] != "codex":
                    fail("provider_unsupported")
                result = inspect_codex(config, diagnostics)
            else:
                result = inspect_subscription(config, diagnostics)
        else:
            request = validate_request(parse(sys.stdin.buffer.read(MAX_BYTES + 1)))
            support = support_for(config, path.parent)
            # Reject drift before authenticated calls, then recheck at output.
            if request["model"] != config["model"] or request["effort"] != config["effort"]:
                fail("request_route_mismatch")
            result = build_result(request, config, collect(config, request["requested_at"]), support)
        if verified_runtime(config) != identity:
            fail("runtime_identity_mismatch")
        sys.stdout.buffer.write(canonical(result) + b"\n")
        return 0
    except ProbeError as exc:
        if inspect:
            print(canonical({"failure": str(exc), "rpc": diagnostics}).decode(), file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
    except Exception:  # noqa: BLE001 -- never expose provider/error/credential data
        print("probe_failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
