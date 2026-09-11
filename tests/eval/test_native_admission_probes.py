"""Synthetic status inputs only; no credentials or live provider calls."""
import copy
import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ukrainian_llm_eval.admission import validate_admission_result

PROBES = Path(__file__).resolve().parents[2] / "tools" / "admission"
sys.path.insert(0, str(PROBES))
common = importlib.import_module("probe_common")
codex = importlib.import_module("codex_status")
subscriptions = importlib.import_module("subscription_status")
probe = importlib.import_module("native_probe")
agy = importlib.import_module("native_agy_status")


def request():
    value = {"schema": "ukrainian-llm-eval.admission-request.v1", "nonce": "a" * 32,
             "requested_at": common.utcnow(), "route_sha256": "b" * 64, "model": "gpt-6-astra",
             "effort": "medium", "condition": "closed_book", "composite_sha256": "c" * 64,
             "requirements": {"input_utf8_bytes": 100, "max_total_input_tokens": 1000,
                 "max_total_output_tokens": 300, "max_output_tokens": 300, "max_tool_calls": 2,
                 "timeout_seconds": 30, "tool_policy_sha256": "d" * 64}}
    value["request_sha256"] = common.digest(value)
    return value


def rehash(value):
    value["request_sha256"] = common.digest({k: v for k, v in value.items() if k != "request_sha256"})


def config():
    route = "b" * 64
    return {"provider": "codex", "model": "gpt-6-astra", "effort": "medium",
        "pricing": {"route_sha256": route, "currency": "USD", "input_micro_usd_per_million_tokens": 0,
                    "output_micro_usd_per_million_tokens": 0, "tool_round_micro_usd": 0},
        "entitlement": {"route_sha256": route, "account_sha256": "e" * 64,
                        "billing_kind": "verified_subscription", "zero_incremental": True,
                        "valid_until": None, "verification": "live_subscription"},
        "capability": {"route_sha256": route, "model": "gpt-6-astra", "effort": "medium",
                       "context_input_tokens": 1200, "max_output_tokens": 400, "max_tool_calls": 3,
                       "timeout_seconds": 40, "tool_policy_sha256": "d" * 64}}


def support():
    return {"conditions": ["closed_book", "sources"], "framing_tokens": 50, "initial_history_tokens": 200,
            "initial_history_verified": True, "input_capacity_basis": "combined_window", "context_window_tokens": 1600,
            "output_headroom_tokens": 400, "output_headroom_verified": True}


def observation():
    return {"account_sha256": "e" * 64, "status_observed_at": common.utcnow()}


def codex_values():
    return ({"account": {"type": "chatgpt", "planType": "pro"}},
        {"accountId": "synthetic-account", "ordinaryUsageAllowed": True, "rateLimitsByLimitId": {
            "codex": {"planType": "pro", "credits": {"hasCredits": False, "unlimited": False},
                      "primary": {"usedPercent": 20}, "secondary": {"usedPercent": 30}}}},
        {"data": [{"model": "gpt-6-astra", "hidden": False,
                   "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high")]}]})


def claude_values():
    return ({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max",
             "orgId": "synthetic-org", "email": "synthetic@example.invalid"},
        {"account": {"uuid": "synthetic-account", "email": "synthetic@example.invalid"},
         "organization": {"uuid": "synthetic-org", "subscription_status": "active",
                          "billing_type": "stripe_subscription", "rate_limit_tier": "default_claude_max_5x"}},
        {"extra_usage": {"is_enabled": False}, "five_hour": {"utilization": 20},
         "seven_day": {"utilization": 30},
         "limits": [{"scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                     "percent": 83, "is_active": True}]})


def agy_values():
    return ({"sub": "synthetic-subject", "email": "synthetic@example.invalid", "email_verified": True},
        {"userStatus": {"email": "synthetic@example.invalid", "userTier": {"id": "g1-ultra-lite-tier"},
            "planStatus": {"planInfo": {"planName": "Pro"}},
            "cascadeModelConfigData": {"clientModelConfigs": [
                {"modelOrAlias": {"model": enum}, "label": label, "quotaInfo": {"remainingFraction": 0.17}}
                for _, enum, label in subscriptions.AGY_MODELS.values()]}}},
        {"response": {"groups": [{"displayName": "Gemini Models", "buckets": [
            {"bucketId": "gemini-weekly", "remainingFraction": 0.9},
            {"bucketId": "gemini-5h", "remainingFraction": 0.17}]}]}})


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_codex_current_account_and_model(effort):
    account = codex.normalize(*codex_values(), "gpt-6-astra", effort, "codex")
    assert account == common.digest({"provider": "openai-chatgpt", "account_id": "synthetic-account"})


@pytest.mark.parametrize("field,value", [("ordinaryUsageAllowed", None), ("ordinaryUsageAllowed", False),
                                          ("accountId", None), ("accountId", "")])
def test_codex_unknown_status_and_identity(field, value):
    values = codex_values()
    values[1][field] = value
    with pytest.raises(common.ProbeError):
        codex.normalize(*values, "gpt-6-astra", "medium", "codex")


@pytest.mark.parametrize("value", [True, None, 0, "false"])
@pytest.mark.parametrize("field", ["hasCredits", "unlimited"])
def test_codex_requires_explicit_no_credit_fallback(field, value):
    values = codex_values()
    values[1]["rateLimitsByLimitId"]["codex"]["credits"][field] = value
    with pytest.raises(common.ProbeError, match="paid_fallback_unknown"):
        codex.normalize(*values, "gpt-6-astra", "medium", "codex")


def test_codex_does_not_substitute_model_or_effort():
    with pytest.raises(common.ProbeError, match="model_unavailable"):
        codex.normalize(*codex_values(), "other-model", "medium", "codex")
    with pytest.raises(common.ProbeError, match="effort_unsupported"):
        codex.normalize(*codex_values(), "gpt-6-astra", "ultra", "codex")


def test_claude_same_account_binding():
    result = subscriptions.normalize_claude(*claude_values(), "claude-fable-5-1")
    assert result == common.digest({"provider": "anthropic-claude", "account_id": "synthetic-account",
                                    "organization_id": "synthetic-org"})
    values = claude_values()
    values[0]["orgId"] = "other-org"
    with pytest.raises(common.ProbeError, match="identity_mismatch"):
        subscriptions.normalize_claude(*values, "claude-fable-5-1")


@pytest.mark.parametrize("extra", [{}, None, {"is_enabled": None}, {"is_enabled": True},
                                    {"is_enabled": 0}, {"is_enabled": "false"}])
def test_claude_missing_null_extra_usage_not_false(extra):
    values = claude_values()
    values[2]["extra_usage"] = extra
    with pytest.raises(common.ProbeError, match="paid_fallback_unknown"):
        subscriptions.normalize_claude(*values, "claude-fable-5-1")


@pytest.mark.parametrize("plan", [None, "unknown", "free", ""])
def test_claude_auth_plus_quota_does_not_prove_paid_subscription(plan):
    values = claude_values()
    values[0]["subscriptionType"] = plan
    with pytest.raises(common.ProbeError, match="subscription_unknown"):
        subscriptions.normalize_claude(*values, "claude-fable-5-1")


@pytest.mark.parametrize("field,value", [
    ("subscription_status", "cancelled"), ("subscription_status", None),
    ("billing_type", "invoice"), ("billing_type", None),
    ("rate_limit_tier", "unknown"), ("rate_limit_tier", "default_claude_max_20x"),
])
def test_claude_requires_current_supported_personal_max_profile(field, value):
    values = claude_values()
    values[1]["organization"][field] = value
    with pytest.raises(common.ProbeError, match="subscription_unknown"):
        subscriptions.normalize_claude(*values, "claude-fable-5-1")


@pytest.mark.parametrize("limits", [None, [], {}, [None], [{"scope": {}, "percent": 20}],
    [{"scope": {"model": {"id": None, "display_name": "Unknown"}}, "percent": 20}],
    [{"scope": {"model": {"id": "other-model", "display_name": "Fable"}}, "percent": 20}],
    [{"scope": {"model": {"id": "claude-fable-5-1", "display_name": "Other"}}, "percent": 20}],
    [{"scope": {"model": "Fable"}, "percent": 20}],
    [{"scope": {"model": {"display_name": "Fable"}}, "percent": 20}],
    [{"scope": {"model": {"id": None, "display_name": "Fable"}, "surface": "unknown"}, "percent": 20}],
])
def test_claude_requires_unambiguous_family_quota(limits):
    values = claude_values()
    values[2]["limits"] = limits
    with pytest.raises(common.ProbeError, match="model_quota_unknown"):
        subscriptions.normalize_claude(*values, "claude-fable-5-1")


@pytest.mark.parametrize("active", [True, False, None])
@pytest.mark.parametrize("percent", [100, 101, -1, None, True, "83"])
def test_claude_every_family_window_must_have_quota(active, percent):
    values = claude_values()
    values[2]["limits"].append({"scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                                "percent": percent, "is_active": active})
    with pytest.raises(common.ProbeError):
        subscriptions.normalize_claude(*values, "claude-fable-5-1")


@pytest.mark.parametrize("window", ["five_hour", "seven_day"])
def test_claude_family_allowance_does_not_override_exhausted_global_window(window):
    values = claude_values()
    values[2][window]["utilization"] = 100
    with pytest.raises(common.ProbeError):
        subscriptions.normalize_claude(*values, "claude-fable-5-1")


def test_claude_observed_global_and_family_shape_with_inactive_windows():
    values = claude_values()
    values[2]["limits"][:0] = [{"scope": {}, "percent": 52, "is_active": False},
                              {"scope": {}, "percent": 57, "is_active": False}]
    values[2]["limits"][-1]["is_active"] = False
    assert subscriptions.normalize_claude(*values, "claude-fable-5-1")
    with pytest.raises(common.ProbeError, match="model_quota_unknown"):
        subscriptions.normalize_claude(*values, "claude-fable-5")


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_agy_same_verified_principal_and_exact_fresh_model(effort):
    result = subscriptions.normalize_agy(*agy_values(), "gemini-3.8-flash-" + effort, effort)
    assert result == common.digest({"provider": "google-antigravity", "issuer": "https://accounts.google.com",
                                   "subject": "synthetic-subject"})


@pytest.mark.parametrize("field,value", [("sub", None), ("email_verified", False), ("email", "other@example.invalid")])
def test_agy_identity_cannot_be_inferred(field, value):
    values = agy_values()
    values[0][field] = value
    with pytest.raises(common.ProbeError):
        subscriptions.normalize_agy(*values, "gemini-3.8-flash-low", "low")


@pytest.mark.parametrize("tier", [{}, {"id": "free-tier"}, {"id": "unknown"}])
def test_agy_legacy_plan_never_establishes_current_subscription(tier):
    values = agy_values()
    values[1]["userStatus"]["userTier"] = tier
    with pytest.raises(common.ProbeError, match="subscription_unknown"):
        subscriptions.normalize_agy(*values, "gemini-3.8-flash-low", "low")


@pytest.mark.parametrize("fraction", [None, False, 0, -1, 1.1, float("nan")])
@pytest.mark.parametrize("window", [0, 1])
def test_agy_all_group_windows_require_quota(fraction, window):
    values = agy_values()
    values[2]["response"]["groups"][0]["buckets"][window]["remainingFraction"] = fraction
    with pytest.raises(common.ProbeError, match="model_quota_unknown"):
        subscriptions.normalize_agy(*values, "gemini-3.8-flash-low", "low")


@pytest.mark.parametrize("mutation", [
    lambda v: v[1]["userStatus"]["cascadeModelConfigData"]["clientModelConfigs"][0]["modelOrAlias"].update(model="wrong"),
    lambda v: v[1]["userStatus"]["cascadeModelConfigData"]["clientModelConfigs"][0]["quotaInfo"].update(remainingFraction=0),
    lambda v: v[2]["response"]["groups"][0].update(buckets=[]),
])
def test_agy_unknown_mapping_or_model_quota_fails(mutation):
    values = agy_values()
    mutation(values)
    with pytest.raises(common.ProbeError):
        subscriptions.normalize_agy(*values, "gemini-3.8-flash-low", "low")


def test_v2_output_is_accepted_by_existing_strict_controller():
    req, cfg = request(), config()
    result = probe.build_result(req, cfg, observation(), support())
    assert result["capability"]["observed"]["required_input_tokens"] == 350
    auth = {"schema": "ukrainian-llm-eval.operator-authorization.v1", "route_sha256": "b" * 64,
            "allow_paid": False, "max_new_spend_micro_usd": 0}
    route = {"route_sha256": "b" * 64, "conditions": ["closed_book", "sources"],
             "billing": {"kind": "verified_subscription", **{k: cfg["pricing"][k] for k in (
                 "input_micro_usd_per_million_tokens", "output_micro_usd_per_million_tokens", "tool_round_micro_usd")}},
             "operator_authorization_sha256": common.digest(auth)}
    for key in ("pricing", "entitlement", "capability"):
        route[key + "_evidence_sha256"] = common.digest(cfg[key])
    receipt = validate_admission_result(result, req, route, cfg, reserved_micro_usd=0,
        remaining_ceiling_micro_usd=0, operator_authorization=auth, max_age_seconds=10)
    assert receipt["incremental_segment_cost_micro_usd"] == 0


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r.update(nonce="wrong"), "invalid_nonce"),
    (lambda r: r.update(requested_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat()), "stale_request"),
    (lambda r: r.update(requested_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat()), "stale_request"),
    (lambda r: r.update(effort="ultra"), "unsupported_effort"),
])
def test_bad_request_before_status(mutation, reason):
    req = request()
    mutation(req)
    rehash(req)
    with pytest.raises(common.ProbeError, match=reason):
        common.validate_request(req)


def test_tampered_request_hash():
    req = request()
    req["requirements"]["input_utf8_bytes"] += 1
    with pytest.raises(common.ProbeError, match="request_hash_mismatch"):
        common.validate_request(req)


def test_old_observation_cannot_be_rebound_to_fresh_nonce():
    req = request()
    obs = observation()
    obs["status_observed_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with pytest.raises(common.ProbeError, match="status_not_fresh"):
        probe.build_result(req, config(), obs, support())


@pytest.mark.parametrize("field", ["max_output_tokens", "max_tool_calls", "timeout_seconds"])
def test_supported_capacity_not_requested_capacity(field):
    req, cfg = request(), config()
    cfg["capability"][field] = req["requirements"][field] - 1
    with pytest.raises(common.ProbeError, match="capacity_insufficient"):
        probe.build_result(req, cfg, observation(), support())


def test_initial_fit_includes_framing_and_history():
    req, cfg = request(), config()
    cfg["capability"]["context_input_tokens"] = 101  # packet alone fits; full input does not
    with pytest.raises(common.ProbeError, match="input_does_not_fit"):
        probe.build_result(req, cfg, observation(), support())


@pytest.mark.parametrize("framing,history,expected", [(50, 200, 350), (250, 0, 350), (50, 0, 150)])
def test_initial_input_sum_and_no_double_output_subtraction(framing, history, expected):
    req, cfg, supp = request(), config(), support()
    supp.update(framing_tokens=framing, initial_history_tokens=history)
    cfg["capability"]["context_input_tokens"] = expected
    supp["context_window_tokens"] = expected + supp["output_headroom_tokens"]
    result = probe.build_result(req, cfg, observation(), supp)
    assert result["capability"]["observed"]["required_input_tokens"] == expected
    assert result["capability"]["observed"]["input_fits"] is True


@pytest.mark.parametrize("field", ["framing_tokens", "initial_history_tokens"])
def test_initial_framing_or_history_overflow(field):
    req, cfg, supp = request(), config(), support()
    supp[field] = 1000
    with pytest.raises(common.ProbeError, match="input_does_not_fit"):
        probe.build_result(req, cfg, observation(), supp)


@pytest.mark.parametrize("field", ["initial_history_verified", "output_headroom_verified"])
@pytest.mark.parametrize("value", [None, False, 1])
def test_zero_initial_history_and_output_headroom_need_explicit_review(field, value):
    req, cfg, supp = request(), config(), support()
    supp.update(initial_history_tokens=0)
    supp[field] = value
    with pytest.raises(common.ProbeError, match="support_proof_unavailable"):
        probe.build_result(req, cfg, observation(), supp)


@pytest.mark.parametrize("window,headroom", [(1600, 399), (1200, 400), (400, 400), (300, 400)])
def test_output_headroom_must_leave_verified_net_input(window, headroom):
    req, cfg, supp = request(), config(), support()
    supp.update(context_window_tokens=window, output_headroom_tokens=headroom)
    with pytest.raises(common.ProbeError, match="output_headroom_invalid"):
        probe.build_result(req, cfg, observation(), supp)


@pytest.mark.parametrize("field", ["context_window_tokens", "output_headroom_tokens", "initial_history_tokens"])
def test_initial_capacity_rejects_missing_source_bound(field):
    supp = support()
    del supp[field]
    with pytest.raises(common.ProbeError):
        probe.build_result(request(), config(), observation(), supp)


def test_old_future_history_contract_cannot_silently_be_reinterpreted():
    supp = support()
    supp["permitted_history_tokens"] = supp.pop("initial_history_tokens")
    with pytest.raises(common.ProbeError, match="legacy_history_bound_rejected"):
        probe.build_result(request(), config(), observation(), supp)


def test_no_fabricated_expiry_or_wrong_account():
    cfg = config()
    cfg["entitlement"]["valid_until"] = "2099-01-01T00:00:00Z"
    with pytest.raises(common.ProbeError, match="expiry_unverified"):
        probe.build_result(request(), cfg, observation(), support())
    req = request()
    obs = observation()
    obs["account_sha256"] = "f" * 64
    with pytest.raises(common.ProbeError, match="entitlement_state_mismatch"):
        probe.build_result(req, config(), obs, support())


@pytest.mark.parametrize("url", ["https://example.invalid/", "http://api.anthropic.com/api/oauth/usage",
    "https://api.anthropic.com/api/oauth/usage?token=secret", "https://api.anthropic.com/api/oauth/usage/",
    "https://cloudcode-pa.googleapis.com/v1internal:onboardUser", "https://oauth2.googleapis.com/token"])
def test_allowlist_rejects_arbitrary_redirect_or_mutation_endpoint(url):
    with pytest.raises(common.ProbeError, match="provider_endpoint_rejected"):
        common.provider_json(url, "synthetic-secret")


def test_redirect_handler_never_forwards_credentials():
    with pytest.raises(common.ProbeError, match="provider_redirect_rejected"):
        common.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid/")


def test_missing_auth_and_secret_free_error(monkeypatch):
    monkeypatch.delenv("ADMISSION_BEARER_TOKEN", raising=False)
    with pytest.raises(common.ProbeError, match="auth_missing"):
        subscriptions.bearer()
    monkeypatch.setattr(common.urllib.request.OpenerDirector, "open",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic-secret")))
    with pytest.raises(common.ProbeError, match="^provider_status_unavailable$") as caught:
        common.provider_json(subscriptions.USERINFO, "synthetic-secret")
    assert "synthetic-secret" not in str(caught.value)


def test_environment_drops_paid_and_code_loading_paths(monkeypatch):
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "HTTPS_PROXY", "PYTHONPATH"):
        monkeypatch.setenv(key, "synthetic-secret")
    assert all("synthetic-secret" != value for value in common.child_env().values())


def test_codex_initialization_order_and_read_only_methods(monkeypatch):
    events = []
    replies = {1: {}, 2: {}, 3: {}, 4: {}}

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            assert "app-server" in argv
        def send(self, value):
            events.append(value)
        def response(self, value):
            assert events[-1]["id"] == value
            return replies[value]
        def close(self):
            events.append({"method": "closed"})

    monkeypatch.setattr(codex, "Process", FakeProcess)
    monkeypatch.setenv("CODEX_HOME", "/synthetic/private")
    codex.collect({"binary": "/synthetic/codex"})
    assert [event["method"] for event in events] == ["initialize", "initialized", "account/read",
                                                    "account/rateLimits/read", "model/list", "closed"]
    assert events[2]["params"] == {"refreshToken": False}


def test_real_subprocess_handshake_without_provider_or_auth(tmp_path, monkeypatch):
    child = tmp_path / "fake_codex.py"
    child.write_text('''import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if "id" in req:
        print(json.dumps({"id":req["id"], "result":{"ok":True}}), flush=True)
''')
    process = common.Process([sys.executable, str(child)], env={}, timeout=3)
    try:
        for number in (1, 2, 3):
            process.send({"id": number, "method": "initialize", "params": {}})
            assert process.response(number) == {"ok": True}
    finally:
        process.close()
    assert process.process.poll() is not None


def test_runtime_and_declared_support_drift(tmp_path):
    runtime = tmp_path / "binary"
    runtime.write_text("synthetic runtime")
    cfg = {"binary": str(runtime), "runtime_files": [{"path": str(runtime),
                                                    "byte_sha256": common.file_hash(runtime)}]}
    assert common.verified_runtime(cfg) == common.digest(cfg["runtime_files"])
    runtime.write_text("changed")
    with pytest.raises(common.ProbeError, match="runtime_identity_mismatch"):
        common.verified_runtime(cfg)


def test_support_must_bind_real_reviewed_artifact_bytes(tmp_path):
    cfg = config()
    cfg["runtime_files"] = [{"path": "/synthetic", "byte_sha256": "f" * 64}]
    artifact = tmp_path / "review.txt"
    artifact.write_text("Synthetic fixture, never actual provider evidence")
    supp = {**support(), "provider": "codex", "model": "gpt-6-astra", "effort": "medium",
            "runtime_files_sha256": common.digest(cfg["runtime_files"]),
            "capability_sha256": common.digest(cfg["capability"]), "pricing_sha256": common.digest(cfg["pricing"]),
            "artifacts": [{"name": artifact.name, "byte_sha256": common.file_hash(artifact)}]}
    for key in ("current_subscription_endpoint_verified", "all_additional_charge_paths_excluded",
                "api_credentials_excluded", "native_control_enforcement_verified",
                "capacity_source_verified", "byte_token_upper_bound_verified"):
        supp[key] = True
    cfg["quota_key"] = "codex"
    supp.update(quota_key="codex", model_quota_mapping_verified=True)
    cfg["support"] = supp
    assert probe.support_for(cfg, tmp_path) == supp
    changed = copy.deepcopy(cfg)
    changed["support"]["all_additional_charge_paths_excluded"] = None
    with pytest.raises(common.ProbeError, match="support_proof_unavailable"):
        probe.support_for(changed, tmp_path)
    artifact.write_text("changed")
    with pytest.raises(common.ProbeError, match="support_evidence_mismatch"):
        probe.support_for(cfg, tmp_path)


def test_complete_probe_from_verified_snapshot(tmp_path, monkeypatch):
    """Actual copied scripts/interpreter + fake native RPC, strict V2 output."""
    import shutil

    from ukrainian_llm_eval.admission_command import invoke_admission

    account, usage, models = codex_values()
    replies = {"initialize": {}, "account/read": account, "account/rateLimits/read": usage, "model/list": models}
    native = tmp_path / "synthetic-codex"
    native.write_text(f"#!{sys.executable}\nimport json,sys\nreplies={replies!r}\n" + '''for line in sys.stdin:
    req=json.loads(line)
    if "id" in req:
        print(json.dumps({"id":req["id"],"result":replies[req["method"]]}),flush=True)
''')
    native.chmod(0o700)
    cfg = config()
    cfg.update(binary=str(native), quota_key="codex",
               runtime_files=[{"path": str(native), "byte_sha256": common.file_hash(native)}])
    cfg["entitlement"]["account_sha256"] = codex.normalize(account, usage, models, cfg["model"], cfg["effort"], "codex")
    artifact = tmp_path / "synthetic-review.txt"
    artifact.write_text("Deterministic fixture only; no provider readiness claimed.")
    cfg["support"] = {**support(), "provider": "codex", "model": cfg["model"], "effort": cfg["effort"],
        "runtime_files_sha256": common.digest(cfg["runtime_files"]),
        "capability_sha256": common.digest(cfg["capability"]), "pricing_sha256": common.digest(cfg["pricing"]),
        "artifacts": [{"name": artifact.name, "byte_sha256": common.file_hash(artifact)}],
        "quota_key": "codex", "model_quota_mapping_verified": True,
        **{key: True for key in ("current_subscription_endpoint_verified", "all_additional_charge_paths_excluded",
          "api_credentials_excluded", "native_control_enforcement_verified", "capacity_source_verified",
          "byte_token_upper_bound_verified")}}
    input_path = tmp_path / "frozen-input.json"
    input_path.write_bytes(common.canonical(cfg))
    executable = tmp_path / "python-runtime"
    shutil.copyfile(sys.executable, executable)
    executable.chmod(0o700)
    files = [(executable, "executable"), (PROBES / "native_probe.py", "script"), (input_path, "runtime_lock"),
             (artifact, "dependency")]
    files.extend((PROBES / name, "dependency") for name in
                 ("probe_common.py", "codex_status.py", "subscription_status.py", "native_agy_status.py"))
    spec = {"schema": "ukrainian-llm-eval.admission-command.v1", "runtime": "python-script-v1",
            "argv": [str(executable), str(PROBES / "native_probe.py"), str(input_path)],
            "declared_files": [{"path": str(path), "byte_sha256": common.file_hash(path), "role": role}
                               for path, role in files],
            "env_names": ["CODEX_HOME"], "timeout_seconds": 10, "stdin_max_bytes": 10000,
            "stdout_max_bytes": 10000, "stderr_max_bytes": 10000}
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "synthetic-home"))
    result = invoke_admission(spec, request())
    assert result["status"] == "success"
    output = common.parse(result["stdout"])
    assert output["schema"] == "ukrainian-llm-eval.admission-result.v2"
    assert output["entitlement"]["state"]["account_sha256"] == cfg["entitlement"]["account_sha256"]
    assert b"synthetic-account" not in result["stdout"]
    assert b"synthetic-review" not in result["stdout"]


def test_http_same_bearer_then_owned_native_status(monkeypatch):
    calls = []
    values = agy_values()
    def fetch(url, token, **kwargs):
        calls.append((url, token))
        return values[0]
    def owned(config, token, reader, requested_at, diagnostics):
        assert token == "synthetic-secret"
        assert common.timestamp(requested_at) <= common.timestamp(common.utcnow())
        return reader(token), values[1], values[2], common.utcnow()
    monkeypatch.setenv("ADMISSION_BEARER_TOKEN", "synthetic-secret")
    monkeypatch.setattr(subscriptions, "provider_json", fetch)
    monkeypatch.setattr(agy, "collect", owned)
    assert subscriptions.collect_agy({})[:3] == values
    assert calls == [(subscriptions.USERINFO, "synthetic-secret")]


def test_native_diagnostic_redacts_error_text_and_unknown_method():
    process = object.__new__(common.Process)
    process.diagnostics = []
    process.diagnostic("received", {"id": 3, "method": "synthetic-secret", "error": {
        "code": -32001, "message": "synthetic-secret", "data": {"token": "synthetic-secret"}}})
    assert process.diagnostics == [{"direction": "received", "method": "unrecognized", "id": 3,
                                    "error_code": -32001, "has_result": False}]


def test_failed_status_cli_does_not_emit_secret(tmp_path):
    import subprocess

    malformed = tmp_path / "secret-input.json"
    malformed.write_text('{"secret": "synthetic-secret"}')
    result = subprocess.run([sys.executable, str(PROBES / "native_probe.py"), str(malformed)],
                            input=b"", capture_output=True, timeout=5, check=False)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"probe_failed\n"


def test_native_user_principal_preserved_without_api_credentials(monkeypatch):
    monkeypatch.setenv("USER", "synthetic-principal")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    env = common.child_env()
    assert env["USER"] == "synthetic-principal"
    assert "ANTHROPIC_API_KEY" not in env


def test_status_stage_survives_failure_without_endpoint_or_secret(monkeypatch):
    diagnostics = []
    monkeypatch.setenv("ADMISSION_BEARER_TOKEN", "synthetic-secret")
    monkeypatch.setattr(subscriptions, "provider_json", lambda *a, **k: common.fail("provider_http_401"))
    with pytest.raises(common.ProbeError, match="provider_http_401"):
        subscriptions.status_read("identity", subscriptions.USERINFO, "synthetic-secret", diagnostics)
    assert diagnostics == [{"stage": "identity", "state": "started"}]



def agy_provision_fixture(tmp_path):
    home = tmp_path / "provisioned"
    home.mkdir(mode=0o700)
    (home / ".gemini").mkdir(mode=0o700)
    path = home / agy.AUTH_PATH
    path.parent.mkdir(mode=0o700)
    path.write_bytes(common.canonical({"auth_method": "consumer", "token": {
        "access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
        "expiry": (datetime.now(UTC) + timedelta(hours=1)).isoformat(), "token_type": "Bearer"}}))
    path.chmod(0o600)
    binary = tmp_path / "native"
    binary.write_text("synthetic-runtime")
    return {"native_home": str(home), "binary": str(binary)}, path


def test_agy_provision_strips_refresh_and_preserves_original(tmp_path, monkeypatch):
    cfg, source = agy_provision_fixture(tmp_path)
    original = source.read_bytes()
    target_home = tmp_path / "isolated"
    target_home.mkdir()
    _, _, target, _ = agy.provision(cfg, "synthetic-access", target_home)
    assert "refresh_token" not in common.parse(target.read_bytes())["token"]
    assert source.read_bytes() == original
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "ambient")
    monkeypatch.setenv("ADMISSION_BEARER_TOKEN", "synthetic-access")
    monkeypatch.setenv("HTTPS_PROXY", "ambient")
    env = agy.environment(target_home)
    assert env["HOME"] == str(target_home)
    assert not {"GOOGLE_APPLICATION_CREDENTIALS", "ADMISSION_BEARER_TOKEN", "HTTPS_PROXY"} & env.keys()


@pytest.mark.parametrize("mutation", [
    lambda v: v["token"].update(access_token="other"),
    lambda v: v["token"].update(expiry="2000-01-01T00:00:00Z"),
    lambda v: v["token"].update(expiry=None),
    lambda v: v["token"].update(unknown_auth="unknown"),
    lambda v: v.update(auth_method="unknown"),
])
def test_agy_provision_rejects_stale_or_unsupported_credentials(tmp_path, mutation):
    cfg, source = agy_provision_fixture(tmp_path)
    value = common.parse(source.read_bytes())
    mutation(value)
    source.write_bytes(common.canonical(value))
    with pytest.raises(common.ProbeError):
        agy.provision(cfg, "synthetic-access", tmp_path / "isolated")


@pytest.mark.parametrize("drift", [None, "credential", "runtime", "timeout"])
def test_agy_owned_collection_reaps_and_rejects_drift(tmp_path, monkeypatch, drift):
    cfg, _source = agy_provision_fixture(tmp_path)
    values = agy_values()
    state = {"closed": False, "calls": []}
    class Child:
        def __init__(self, binary, home, workspace, deadline):
            state["home"] = home
            assert set(common.parse((home / agy.AUTH_PATH).read_bytes())["token"]) == {"access_token", "expiry", "token_type"}
        def remaining(self):
            if drift == "timeout":
                common.fail("native_timeout")
        def owned(self): pass
        def drain(self, wait=0): pass
        def ports(self): return {("127.0.0.1", 12345)}
        def rpc(self, endpoint, method, body):
            state["calls"].append((method, body))
            if method == "RetrieveUserQuotaSummary": return values[2]
            if drift == "credential": (state["home"] / agy.AUTH_PATH).write_text("changed")
            if drift == "runtime": Path(cfg["binary"]).write_text("changed")
            return values[1]
        def close(self): state["closed"] = True
    monkeypatch.setattr(agy, "OwnedNative", Child)
    if drift:
        with pytest.raises(common.ProbeError):
            agy.collect(cfg, "synthetic-access", lambda _: values[0], common.utcnow())
    else:
        result = agy.collect(cfg, "synthetic-access", lambda _: values[0], common.utcnow())
        assert result[:3] == values
        assert state["calls"][0] == ("RetrieveUserQuotaSummary", {"forceRefresh": True})
        assert state["calls"][1][0] == "GetUserStatus"
    assert state["closed"]
    assert not state["home"].exists()


def test_agy_real_child_timeout_is_reaped(tmp_path):
    import time
    child_script = tmp_path / "sleep-native"
    child_script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    child_script.chmod(0o700)
    child = agy.OwnedNative(str(child_script), tmp_path, tmp_path, time.monotonic() - 1)
    try:
        with pytest.raises(common.ProbeError, match="native_timeout"):
            child.remaining()
    finally:
        child.close()
    assert child.process.poll() is not None


def test_agy_ports_only_owned_loopback_and_correct_ipv6(monkeypatch):
    import io
    from types import SimpleNamespace
    child = object.__new__(agy.OwnedNative)
    child.process = SimpleNamespace(pid=123)
    child.env = {}
    child.owned = lambda: None
    child.remaining = lambda: 5
    class Listing:
        def __init__(self, argv, **kwargs):
            assert argv[argv.index("-p") + 1] == "123"
            self.process = SimpleNamespace(stdin=io.BytesIO())
        def chunks(self): return [b"p123\nn127.0.0.1:12\nn[::1]:13\nn*:14\nn192.0.2.1:15\n"]
        def close(self): pass
    monkeypatch.setattr(agy, "Process", Listing)
    assert child.ports() == {("127.0.0.1", 12), ("[::1]", 13)}


def test_agy_rpc_requires_current_owned_endpoint_and_allowlisted_method():
    child = object.__new__(agy.OwnedNative)
    child.ports = lambda: {("127.0.0.1", 12)}
    for endpoint, method in [(("127.0.0.1", 13), "GetUserStatus"), (("127.0.0.1", 12), "RefreshToken")]:
        with pytest.raises(common.ProbeError, match="native_endpoint_rejected"):
            child.rpc(endpoint, method, {})


def test_agy_rpc_disables_proxy_and_rejects_redirect_response(monkeypatch):
    import io
    import urllib.request
    child = object.__new__(agy.OwnedNative)
    child.ports = lambda: {("[::1]", 12)}
    child.owned = lambda: None
    child.drain = lambda: None
    child.remaining = lambda: 5
    class Response(io.BytesIO):
        status = 200
        def __init__(self, body):
            super().__init__(body)
            self.headers = {}
        def geturl(self): return "https://example.invalid/redirect"
    class Opener:
        def open(self, request, **kwargs):
            assert request.full_url.startswith("https://[::1]:12/")
            assert request.get_header("Connect-protocol-version") == "1"
            assert request.get_header("Authorization") is None
            return Response(b"{}")
    def opener(*handlers):
        assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
        assert any(isinstance(h, common.NoRedirect) for h in handlers)
        return Opener()
    monkeypatch.setattr(agy.urllib.request, "build_opener", opener)
    with pytest.raises(common.ProbeError, match="native_response_rejected"):
        child.rpc(("[::1]", 12), "GetUserStatus", {})



def native_input_support():
    supp = support()
    del supp["output_headroom_tokens"]
    del supp["output_headroom_verified"]
    supp.update(input_capacity_basis="native_usable_input", context_window_tokens=272000,
                effective_context_window_percent=95, native_usable_input_verified=True)
    return supp


def test_native_usable_input_exact_source_derivation_without_double_subtraction():
    cfg, supp = config(), native_input_support()
    cfg["capability"]["context_input_tokens"] = 258400
    cfg["capability"]["max_output_tokens"] = 1000000  # Independent supported-output semantics.
    assert probe.initial_capacity(cfg["capability"], supp) == (50, 200, 258400)
    cfg["capability"]["context_input_tokens"] += 1
    with pytest.raises(common.ProbeError, match="native_input_capacity_invalid"):
        probe.initial_capacity(cfg["capability"], supp)


@pytest.mark.parametrize("percent", [None, 0, 101, True, 95.0])
def test_native_usable_input_rejects_unknown_or_invalid_percent(percent):
    supp = native_input_support()
    supp["effective_context_window_percent"] = percent
    with pytest.raises(common.ProbeError):
        probe.initial_capacity(config()["capability"], supp)


@pytest.mark.parametrize("field,value", [("output_headroom_tokens", 400), ("output_headroom_verified", True)])
def test_native_input_cannot_mix_combined_window_fields(field, value):
    supp = native_input_support()
    supp[field] = value
    with pytest.raises(common.ProbeError, match="input_capacity_basis_conflict"):
        probe.initial_capacity(config()["capability"], supp)


def test_native_input_semantics_need_reviewed_source_and_explicit_basis():
    supp = native_input_support()
    supp["native_usable_input_verified"] = False
    with pytest.raises(common.ProbeError, match="support_proof_unavailable"):
        probe.initial_capacity(config()["capability"], supp)
    for basis in (None, "unknown"):
        supp["input_capacity_basis"] = basis
        with pytest.raises(common.ProbeError, match="input_capacity_basis_unknown"):
            probe.initial_capacity(config()["capability"], supp)
    supp = support()
    supp["effective_context_window_percent"] = 95
    with pytest.raises(common.ProbeError, match="input_capacity_basis_conflict"):
        probe.initial_capacity(config()["capability"], supp)
