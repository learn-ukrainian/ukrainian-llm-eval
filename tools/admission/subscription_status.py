"""Authenticated Claude and Antigravity status, without refresh or onboarding."""
import os

from probe_common import available_percent, child_env, digest, fail, native_json, provider_json, text, utcnow

CLAUDE = "https://api.anthropic.com/api/oauth/"
AGY = "https://cloudcode-pa.googleapis.com/v1internal:"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def bearer():
    value = os.environ.get("ADMISSION_BEARER_TOKEN")
    if not value:
        fail("auth_missing")
    return text(value)


def status_read(stage, url, token, diagnostics, **kwargs):
    if diagnostics is not None:
        diagnostics.append({"stage": stage, "state": "started"})
    result = provider_json(url, token, **kwargs)
    if diagnostics is not None:
        diagnostics.append({"stage": stage, "state": "completed"})
    return result


def collect_claude(config, diagnostics=None):
    token = bearer()
    auth = native_json([config["binary"], "auth", "status", "--json"], child_env())
    profile = status_read("profile", CLAUDE + "profile", token, diagnostics)
    usage = status_read("usage", CLAUDE + "usage", token, diagnostics, headers={"anthropic-beta": "oauth-2025-04-20"})
    return auth, profile, usage, utcnow()


def normalize_claude(auth, profile, usage, model):
    if not all(isinstance(value, dict) for value in (auth, profile, usage)):
        fail("subscription_unknown")
    if auth.get("loggedIn") is not True or auth.get("authMethod") != "claude.ai":
        fail("subscription_auth_required")
    if auth.get("subscriptionType") != "max":
        fail("subscription_unknown")
    account = profile.get("account") or {}
    organization = profile.get("organization") or {}
    if not isinstance(account, dict) or not isinstance(organization, dict):
        fail("subscription_unknown")
    if (organization.get("subscription_status") != "active"
            or organization.get("billing_type") != "stripe_subscription"
            or organization.get("rate_limit_tier") != "default_claude_max_5x"):
        fail("subscription_unknown")
    if model != "claude-fable-5-1":
        fail("model_quota_unknown")
    account_id = text(account.get("uuid"))
    org_id = text(organization.get("uuid"))
    # Exact same-account correlation, never a token digest or guessed identity.
    if auth.get("orgId") != org_id or auth.get("email") != account.get("email"):
        fail("subscription_identity_mismatch")
    text(account.get("email"))
    extra_usage = usage.get("extra_usage")
    if not isinstance(extra_usage, dict) or extra_usage.get("is_enabled") is not False:
        fail("paid_fallback_unknown")
    windows = [usage.get("five_hour"), usage.get("seven_day")]
    if not all(isinstance(window, dict) for window in windows):
        fail("quota_unknown")
    for window in windows:
        available_percent(window.get("utilization"))
    # Provider scope display_name=Fable denotes this exact selected model's
    # family allowance. Unknown families/surfaces need reviewed mapping first.
    limits = usage.get("limits")
    if not isinstance(limits, list) or not limits:
        fail("model_quota_unknown")
    family_found = False
    for limit in limits:
        if not isinstance(limit, dict):
            fail("model_quota_unknown")
        scope = limit.get("scope")
        if scope is None or scope == {}:
            available_percent(limit.get("percent"))
            continue
        if not isinstance(scope, dict) or set(scope) - {"model", "surface"}:
            fail("model_quota_unknown")
        scoped_model = scope.get("model")
        if scope.get("surface") is not None or not isinstance(scoped_model, dict):
            fail("model_quota_unknown")
        if "id" not in scoped_model or set(scoped_model) - {"id", "display_name"}:
            fail("model_quota_unknown")
        scoped_id = scoped_model.get("id")
        name = scoped_model.get("display_name")
        if not ((scoped_id is None and name == "Fable")
                or (scoped_id == model and name in (None, "Fable"))):
            fail("model_quota_unknown")
        family_found = True
        # is_active is a UI flag, not an exemption from a quota window.
        available_percent(limit.get("percent"))
    if not family_found:
        fail("model_quota_unknown")
    return digest({"provider": "anthropic-claude", "account_id": account_id, "organization_id": org_id})


def collect_agy(config, diagnostics=None):
    token = bearer()  # One credential instance for identity, plan, model and quota.
    identity = status_read("identity", USERINFO, token, diagnostics)
    plan = status_read("plan", AGY + "loadCodeAssist", token, diagnostics, body={"metadata": {
        "ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}})
    project = plan.get("cloudaicompanionProject")
    if isinstance(project, dict):
        project = project.get("id") or project.get("projectId")
    project = text(project)
    models = status_read("models", AGY + "fetchAvailableModels", token, diagnostics, body={"project": project})
    quota = status_read("quota", AGY + "retrieveUserQuota", token, diagnostics, body={"project": project})
    return identity, plan, models, quota, project, utcnow()


def normalize_agy(identity, plan, models, quota, project, model):
    subject = text(identity.get("sub"))
    text(project)
    observed_project = plan.get("cloudaicompanionProject")
    if isinstance(observed_project, dict):
        observed_project = observed_project.get("id") or observed_project.get("projectId")
    if observed_project != project:
        fail("subscription_identity_mismatch")
    # allowedTiers and paidTier are onboarding choices, never current proof.
    tier = (plan.get("currentTier") or {}).get("id")
    if tier != "standard-tier":
        fail("subscription_unknown")
    model_info = (models.get("models") or {}).get(model)
    if not isinstance(model_info, dict):
        fail("model_unavailable")
    fraction = (model_info.get("quotaInfo") or {}).get("remainingFraction")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
        fail("model_quota_unknown")
    matching = [bucket for bucket in quota.get("buckets", []) if bucket.get("modelId") == model]
    if not matching:
        fail("model_quota_unknown")
    for bucket in matching:
        fraction = bucket.get("remainingFraction")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
            fail("model_quota_unknown")
    return digest({"provider": "google-antigravity", "issuer": "https://accounts.google.com",
                   "subject": subject, "project_id": project})
