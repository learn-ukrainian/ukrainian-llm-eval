"""Authenticated Claude and Antigravity status, without refresh or onboarding."""
import math
import os

import native_agy_status
from probe_common import (
    available_percent,
    child_env,
    digest,
    fail,
    native_json,
    provider_json,
    provider_pace_after_admission,
    text,
    utcnow,
)

CLAUDE = "https://api.anthropic.com/api/oauth/"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

# Max-subscription Claude Code model ids → provider family quota display_name.
# Never map these to Anthropic API keys; admission is subscription-native only.
CLAUDE_SUBSCRIPTION_MODELS = {
    "claude-fable-5-1": "Fable",
    "claude-sonnet-5": "Sonnet",
    "claude-opus-5": "Opus",
}


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
    # Steady cadence across separate admission processes; avoid OAuth bursts.
    provider_pace_after_admission()
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
    family_name = CLAUDE_SUBSCRIPTION_MODELS.get(model)
    if family_name is None:
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
    # Provider scope display_name denotes a family allowance when present.
    # Fable has an explicit Max family pool and must appear. Sonnet/Opus share
    # the non-Fable Max capacity: if a matching family window is advertised,
    # enforce it; if absent, globals already checked above are sufficient.
    # Other known Max families may appear and must be excluded. Unknown
    # families/surfaces still fail closed.
    limits = usage.get("limits")
    if not isinstance(limits, list) or not limits:
        fail("model_quota_unknown")
    known_families = set(CLAUDE_SUBSCRIPTION_MODELS.values())
    known_ids = set(CLAUDE_SUBSCRIPTION_MODELS)
    # Fable is the only Max family that always requires its own scoped window.
    require_family = family_name == "Fable"
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
        if scoped_id == model:
            if name not in (None, family_name):
                fail("model_quota_unknown")
            family_found = True
            available_percent(limit.get("percent"))
            continue
        if scoped_id is None and name == family_name:
            family_found = True
            # is_active is a UI flag, not an exemption from a quota window.
            available_percent(limit.get("percent"))
            continue
        if name in known_families and name != family_name:
            continue
        if scoped_id in known_ids and scoped_id != model:
            expected = CLAUDE_SUBSCRIPTION_MODELS[scoped_id]
            if name not in (None, expected):
                fail("model_quota_unknown")
            continue
        fail("model_quota_unknown")
    if require_family and not family_found:
        fail("model_quota_unknown")
    return digest({"provider": "anthropic-claude", "account_id": account_id, "organization_id": org_id})


def collect_agy(config, diagnostics=None, *, requested_at=None):
    token = bearer()
    return native_agy_status.collect(config, token,
        lambda selected: status_read("identity", USERINFO, selected, diagnostics),
        requested_at or utcnow(), diagnostics)


AGY_MODELS = {
    "low": ("gemini-3.8-flash-low", "MODEL_PLACEHOLDER_M320", "Gemini 3.8 Flash (Low)"),
    "medium": ("gemini-3.8-flash-medium", "MODEL_PLACEHOLDER_M319", "Gemini 3.8 Flash (Medium)"),
    "high": ("gemini-3.8-flash-high", "MODEL_PLACEHOLDER_M318", "Gemini 3.8 Flash (High)"),
}


def remaining_fraction(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 1):
        fail("model_quota_unknown")


def normalize_agy(identity, status, quota, model, effort):
    if not all(isinstance(value, dict) for value in (identity, status, quota)):
        fail("subscription_unknown")
    subject = text(identity.get("sub"))
    email = text(identity.get("email"))
    user = status.get("userStatus")
    if not isinstance(user, dict) or identity.get("email_verified") is not True or user.get("email") != email:
        fail("subscription_identity_mismatch")
    # userTier is authoritative; legacy planInfo Pro and onboarding choices are not.
    if not isinstance(user.get("userTier"), dict) or user["userTier"].get("id") != "g1-ultra-lite-tier":
        fail("subscription_unknown")
    selected = AGY_MODELS.get(effort)
    if selected is None or model != selected[0]:
        fail("model_unavailable")
    model_data = user.get("cascadeModelConfigData")
    models = model_data.get("clientModelConfigs") if isinstance(model_data, dict) else None
    if not isinstance(models, list):
        fail("model_unavailable")
    matches = []
    for row in models:
        if not isinstance(row, dict) or not isinstance(row.get("modelOrAlias"), dict):
            fail("model_unavailable")
        enum = row["modelOrAlias"].get("model")
        if enum == selected[1] or row.get("label") == selected[2]:
            if enum != selected[1] or row.get("label") != selected[2]:
                fail("model_unavailable")
            matches.append(row)
    if len(matches) != 1 or not isinstance(matches[0].get("quotaInfo"), dict):
        fail("model_quota_unknown")
    remaining_fraction(matches[0]["quotaInfo"].get("remainingFraction"))
    response = quota.get("response")
    groups = response.get("groups") if isinstance(response, dict) else None
    if not isinstance(groups, list) or any(not isinstance(group, dict) for group in groups):
        fail("model_quota_unknown")
    matching = [group for group in groups if group.get("displayName") == "Gemini Models"]
    if len(matching) != 1 or not isinstance(matching[0].get("buckets"), list):
        fail("model_quota_unknown")
    found = set()
    for bucket in matching[0]["buckets"]:
        if not isinstance(bucket, dict) or bucket.get("bucketId") not in {"gemini-weekly", "gemini-5h"}:
            fail("model_quota_unknown")
        remaining_fraction(bucket.get("remainingFraction"))
        found.add(bucket["bucketId"])
    if found != {"gemini-weekly", "gemini-5h"}:
        fail("model_quota_unknown")
    return digest({"provider": "google-antigravity", "issuer": "https://accounts.google.com", "subject": subject})
