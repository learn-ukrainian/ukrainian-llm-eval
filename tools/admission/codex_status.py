"""Read-only initialized native Codex app-server status; no thread/turn RPCs."""
from probe_common import Process, available_percent, child_env, digest, fail, text, utcnow


def collect(config, diagnostics=None):
    env = child_env()
    if not env.get("CODEX_HOME"):
        fail("codex_auth_provisioning_missing")
    # The caller provides the evaluator's isolated, reviewed subscription home.
    # Disable automatic update checks; no login or token-refresh RPC is sent.
    process = Process([config["binary"], "app-server", "--stdio", "-c", 'forced_login_method="chatgpt"',
                       "-c", "check_for_update_on_startup=false"], env=env, timeout=60, diagnostics=diagnostics)
    try:
        process.send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "ukrainian_eval_admission", "version": "1"},
            "capabilities": {"experimentalApi": True}}})
        process.response(1)
        process.send({"method": "initialized", "params": {}})
        results = []
        for number, method, params in (
            (2, "account/read", {"refreshToken": False}),
            (3, "account/rateLimits/read", {}),
            (4, "model/list", {"limit": 100, "includeHidden": False}),
        ):
            process.send({"id": number, "method": method, "params": params})
            result = process.response(number)
            results.append(result)
            if diagnostics is not None and method == "account/read":
                account = result.get("account") or {}
                diagnostics.append({"stage": "account_read", "chatgpt": account.get("type") == "chatgpt",
                    "subscription_plan": account.get("planType") if account.get("planType") in
                        {"plus", "pro", "prolite", "team", "free", "unknown"} else "other"})
        return (*results, utcnow())
    finally:
        process.close()


def normalize(account_reply, usage, catalog, model, effort, quota_key):
    account = account_reply.get("account") or {}
    if account.get("type") != "chatgpt" or account.get("planType") not in {"plus", "pro", "prolite", "team"}:
        fail("subscription_unknown")
    # This field is backend permission for included usage. A percentage alone
    # must never repair null/false permission or prove subscription entitlement.
    if usage.get("ordinaryUsageAllowed") is not True:
        fail("subscription_ineligible")
    account_id = text(usage.get("accountId"))
    buckets = usage.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or quota_key != "codex" or "codex" not in buckets:
        fail("model_quota_unknown")
    rows = catalog.get("data")
    if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
        fail("model_unavailable")
    matches = [item for item in rows if item.get("model") == model]
    if len(matches) != 1 or matches[0].get("hidden") is not False:
        fail("model_unavailable")
    if effort not in {item.get("reasoningEffort") for item in matches[0].get("supportedReasoningEfforts", [])}:
        fail("effort_unsupported")
    identities = {}
    slugs = {text(row.get("model")).casefold() for row in rows}
    for row in rows:
        slug = text(row.get("model"))
        for name in (slug, row.get("displayName")):
            if name is not None:
                identities.setdefault(text(name).casefold(), set()).add(slug)
    selected = [("codex", buckets["codex"])]
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            fail("model_quota_unknown")
        if bucket.get("limitId") not in (None, key):
            fail("model_quota_unknown")
        if key == "codex":
            continue
        slug, name = bucket.get("normalModelSlug"), bucket.get("limitName")
        mapped = set()
        for label in (slug, name):
            if label is not None:
                mapped.update(identities.get(text(label).casefold(), set()))
        # A known normal-model slug may accompany a non-model quota label
        # such as gpt-reserve. Unknown slugs or conflicting identities are not
        # grounds to discard a potentially applicable constraint.
        if ((slug is not None and text(slug).casefold() not in slugs) or len(mapped) != 1):
            fail("model_quota_unknown")
        if model in mapped:
            selected.append((key, bucket))
    for _key, quota in selected:
        if quota.get("planType") != account["planType"]:
            fail("subscription_identity_mismatch")
        if quota.get("rateLimitReachedType") is not None or quota.get("spendControlReached") not in (None, False):
            fail("quota_exhausted")
        credits = quota.get("credits") or {}
        if not isinstance(credits, dict):
            fail("paid_fallback_unknown")
        if credits.get("hasCredits") is not False or credits.get("unlimited") is not False:
            fail("paid_fallback_unknown")
        windows = [quota.get("primary"), quota.get("secondary")]
        if not any(isinstance(window, dict) for window in windows):
            fail("quota_unknown")
        for window in windows:
            if window is not None:
                if not isinstance(window, dict):
                    fail("quota_unknown")
                available_percent(window.get("usedPercent"))
    return digest({"provider": "openai-chatgpt", "account_id": account_id})
