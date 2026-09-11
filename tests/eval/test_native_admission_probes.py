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
    return {"conditions": ["closed_book", "sources"], "framing_tokens": 50, "permitted_history_tokens": 200}


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
         "organization": {"uuid": "synthetic-org"}},
        {"extra_usage": {"is_enabled": False}, "five_hour": {"utilization": 20},
         "seven_day": {"utilization": 30}})


def agy_values():
    return ({"sub": "synthetic-subject"},
        {"currentTier": {"id": "standard-tier"}, "cloudaicompanionProject": "synthetic-project"},
        {"models": {"gemini-3.1-pro": {"quotaInfo": {"remainingFraction": 0.5}}}},
        {"buckets": [{"modelId": "gemini-3.1-pro", "remainingFraction": 0.5}]}, "synthetic-project")


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
    result = subscriptions.normalize_claude(*claude_values(), "claude-fable-5")
    assert result == common.digest({"provider": "anthropic-claude", "account_id": "synthetic-account",
                                    "organization_id": "synthetic-org"})
    values = claude_values()
    values[0]["orgId"] = "other-org"
    with pytest.raises(common.ProbeError, match="identity_mismatch"):
        subscriptions.normalize_claude(*values, "claude-fable-5")


@pytest.mark.parametrize("extra", [{}, None, {"is_enabled": None}, {"is_enabled": True},
                                    {"is_enabled": 0}, {"is_enabled": "false"}])
def test_claude_missing_null_extra_usage_not_false(extra):
    values = claude_values()
    values[2]["extra_usage"] = extra
    with pytest.raises(common.ProbeError, match="paid_fallback_unknown"):
        subscriptions.normalize_claude(*values, "claude-fable-5")


@pytest.mark.parametrize("plan", [None, "unknown", "free", ""])
def test_claude_auth_plus_quota_does_not_prove_paid_subscription(plan):
    values = claude_values()
    values[0]["subscriptionType"] = plan
    with pytest.raises(common.ProbeError, match="subscription_unknown"):
        subscriptions.normalize_claude(*values, "claude-fable-5")


def test_agy_same_bearer_subject_and_project_binding():
    result = subscriptions.normalize_agy(*agy_values(), "gemini-3.1-pro")
    assert result == common.digest({"provider": "google-antigravity", "issuer": "https://accounts.google.com",
                                   "subject": "synthetic-subject", "project_id": "synthetic-project"})
    values = agy_values()
    values[0].pop("sub")
    values[0]["email"] = "not-an-identity@example.invalid"
    with pytest.raises(common.ProbeError):
        subscriptions.normalize_agy(*values, "gemini-3.1-pro")


@pytest.mark.parametrize("tier", [{}, {"id": "free-tier"}, {"id": "unknown"}])
def test_agy_onboarding_tiers_never_establish_current_subscription(tier):
    values = agy_values()
    values[1]["currentTier"] = tier
    values[1]["paidTier"] = {"id": "standard-tier"}
    values[1]["allowedTiers"] = [{"id": "standard-tier"}]
    with pytest.raises(common.ProbeError, match="subscription_unknown"):
        subscriptions.normalize_agy(*values, "gemini-3.1-pro")


@pytest.mark.parametrize("fraction", [None, False, 0, -1, 1.1, float("nan")])
def test_agy_model_quota_requires_fresh_concrete_capacity(fraction):
    values = agy_values()
    values[3]["buckets"][0]["remainingFraction"] = fraction
    with pytest.raises(common.ProbeError, match="model_quota_unknown"):
        subscriptions.normalize_agy(*values, "gemini-3.1-pro")


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


def test_fit_includes_framing_and_history():
    req, cfg = request(), config()
    cfg["capability"]["context_input_tokens"] = 101  # packet alone fits; full input does not
    with pytest.raises(common.ProbeError, match="input_does_not_fit"):
        probe.build_result(req, cfg, observation(), support())


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
                 ("probe_common.py", "codex_status.py", "subscription_status.py"))
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


def test_http_same_bearer_and_fixed_status_sequence(monkeypatch):
    calls = []
    values = agy_values()
    replies = {subscriptions.USERINFO: values[0], subscriptions.AGY + "loadCodeAssist": values[1],
               subscriptions.AGY + "fetchAvailableModels": values[2], subscriptions.AGY + "retrieveUserQuota": values[3]}

    def fetch(url, token, **kwargs):
        calls.append((url, token, kwargs))
        return replies[url]

    monkeypatch.setenv("ADMISSION_BEARER_TOKEN", "synthetic-secret")
    monkeypatch.setattr(subscriptions, "provider_json", fetch)
    result = subscriptions.collect_agy({})
    assert result[:5] == values
    assert {token for _, token, _ in calls} == {"synthetic-secret"}
    assert [url for url, _, _ in calls] == list(replies)
    assert calls[-1][2] == {"body": {"project": "synthetic-project"}}


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
