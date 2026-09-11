# Trusted live admission integrations

The admission modules implement a controller boundary for user-owned provider
probes. The segmented research scheduler and `run-research` CLI now use this
validated path when supplied with explicit command-spec and operator-
authorization maps. An echo callback or constant-hash fixture is suitable for
deterministic tests only; it is not a valid inaugural provider probe.

## Command contract

`admission_command` accepts only an explicitly supplied trusted command spec
with schema `ukrainian-llm-eval.admission-command.v1` and runtime
`python-script-v1`. Do not discover or execute commands from downloaded
benchmark data, manifests, or result archives.

The spec declares absolute executable/script paths, a runtime lock and optional
dependency files. Every declared file has a byte SHA-256. The implementation
opens and verifies the files, copies those verified bytes into a private
snapshot, and executes the copied Python interpreter and script directly.
Arguments are not shell commands. Only explicitly named environment variables
are passed; code-loading environment variables are rejected.

Each spec supplies time and stdin/stdout/stderr byte limits within defensive
protocol ceilings. Timeout and output overflow kill the process group. Failure
results contain normalized status, byte counts and hashes, with no raw streams.
Successful stdout is passed to the strict claims validator. The probe receives
safe requirements and identity information, not packet text, grading keys,
references, previous responses or reservation amounts.

Identity covers the declared files. It does not freeze shared libraries, the
standard library, the operating system or undeclared imports. The command is a
trusted integration, not a sandbox for hostile programs. Unsupported runtime
shapes fail closed. A probe must use real read-only provider sources to support
its claims; file hashes alone cannot prove those claims true.

## Fresh claims and authorization

`admission.build_admission_request` creates a fresh nonce and canonical request
hash. The response schema `ukrainian-llm-eval.admission-result.v1` binds that
nonce and hash, an observation time, and exactly three records: `pricing`,
`entitlement` and `capability`. Unknown or additional fields are rejected.

Each record separates frozen `state`/`state_sha256` from fresh `observed`
claims. Rates, account/route identity, entitlement validity and supported
limits stay bound to the plan. Quota or credit balances can change between
segments and are checked anew. Timestamps must be within the request's fresh
observation window; entitlement expiry is checked separately.

The controller independently computes conservative token/tool cost using
integer micro-USD and compares the observed quote against that arithmetic,
the frozen reservation and remaining ceiling. Existing credit must cover the
quoted provider charge while incremental spend remains zero. Unknown pricing,
unknown fit, unhealthy routes, insufficient credit, changed identity or limits,
expired entitlement and unauthorized incremental new-money cost all fail
closed.

### Native subscriptions without a published expiry

An integration may explicitly select `ukrainian-llm-eval.admission-result.v2`
for `verified_subscription` routes. V1 is unchanged and still requires a future
provider expiry. V2 adds `verification: "live_subscription"` to the frozen
entitlement state and permits `valid_until: null` when the provider does not
expose that date. A known date must still be honored. A credential expiry,
quota reset, or locally chosen freshness deadline is never a substitute.

V2 entitlement observations add `subscription_status: "active"`,
`paid_fallback_enabled: false`, and `status_observed_at`. The trusted command
must obtain these from current authenticated provider evidence and verified
native billing controls for each admission request. The status observation
must fall between that request's timestamp and the result's observation time,
within the existing freshness window. Cached canary success, a configured
subscription label, or a timestamp added to old account data is insufficient.
Inactive/unknown status, enabled/unknown paid fallback, stale observations,
identity drift, and an exposed expired entitlement fail. V2 is rejected for
metered and existing-credit routes.

The state hash changes when this mode is selected, so an existing frozen V1
route cannot silently acquire the relaxed date representation. Separate
operator authorization, pricing, capability, request/nonce binding and the
experiment's spending ceiling still apply. The preserved admission result
retains the explicit null expiry and verification mode; its hash remains bound
by the normal receipt. These checks validate a trusted command's claims, not
the honesty of arbitrary command code. Live provider integrations and their
installed behavior require review before study admission.

Admission binds the complete request-budget mechanism SHA-256 into its
composite identity. Its receipt also retains the account identity and observed
existing-credit balance needed by the request controller. Admission remains a
separate check: its prompt-size observation does not replace exact counting of
the serialized request and cumulative tool history under v1, or the explicitly
labeled documented provider-context upper bound under v2. See [request-level
budget control](request-budget.md).

Operator authorization is separate from entitlement. Its strict record binds
the route and the permission and ceiling for incremental new-money charges;
the plan must pin its canonical hash. Supplying that exact route-bound record
is the explicit authorization to execute the route. The legacy v1 record has
one `max_new_spend_micro_usd` field and always means the total reservation for
the route, including when the plan happens to use a sequential ledger. A v1
record is never reinterpreted as a per-segment grant.

To authorize per-segment reservations under `sequential_shared_cap`, use the
distinct v2 record. A v1 grant remains valid if the full route reservation fits
its total ceiling. The v2 record binds the exact
`digest(plan.spending_policy)` and names its separate
`max_segment_new_spend_micro_usd` limit. The policy digest covers the shared
ledger's experiment-wide cap; the v2 field covers each immutable metered
segment. Both `allow_paid` and `route_sha256` remain hash-bound. For example,
the legacy whole-route form is:

```json
{"schema":"ukrainian-llm-eval.operator-authorization.v1","route_sha256":"<route>","allow_paid":true,"max_new_spend_micro_usd":5000000}
```

For a sequential policy with a 5,000,000 micro-USD shared cap and a 250,000
micro-USD segment ceiling, create a separate v2 record:

```json
{"schema":"ukrainian-llm-eval.operator-authorization.v2","route_sha256":"<route>","allow_paid":true,"max_segment_new_spend_micro_usd":250000,"spending_policy_sha256":"<digest-of-exact-plan-spending-policy>"}
```

`allow_paid: false` with a zero new-spend ceiling is therefore the expected
record for an explicitly selected subscription or existing-credit route: it
permits use of that route while forbidding new metered charges. It is not a
route-disable switch.

An account subscription or balance alone is not permission to use it: without
the matching route-bound record, admission fails. Existing-credit consumption
is separately limited by the frozen route bounds, fresh balance verification
and retained request-budget commitments. Integration enforces the authorized
new-spend total across the entire frozen schedule and each segment.

`invoke_validated_admission` retains the safe request and command result in a
private append-only evidence store. Accepted claims and their full response
hash are preserved. Rejected output is retained only as counts, hashes and a
normalized failure, avoiding arbitrary credential-bearing streams. Interrupted
probe attempts remain visible rather than being silently discarded.

## Remaining live-experiment gates

The scheduler binds the command/composite identity, operator authorization,
stable state hashes and fresh admission receipt to each segment's execution
receipt. The public run command uses this validated path. The inaugural routes
still require real pricing, entitlement, context/output and tool-control
probes; mock subprocess tests establish controller behavior only. Independent
review and installed end-to-end proof remain required before release. Real
metered and existing-credit routes must supply the implemented request-budget
integration. The repository does not bundle or claim a verified provider
tokenizer; route-specific mechanism evidence remains an inaugural-run gate.

## Bundled native subscription collectors

`tools/admission/native_probe.py` is a standard-library trusted integration for
Codex/Astra, Claude/Fable and Antigravity/Gemini, at low, medium and high effort.
It implements the existing request-v1/result-v2 contracts; it does not run a
candidate, onboard an account, request token refresh, or discover credentials. Copy
these four source files from the reviewed checkout when assembling a command
spec; they are operator tools, not dependencies of the installed evaluator.
Declare `native_probe.py` as the script, the other three Python files as
`dependency`, and the frozen input JSON as `runtime_lock`. Pass its absolute
path as argv[2]. Declare each evidence artifact described below as another
`dependency`; the command runner snapshots them with unchanged basenames.

The probe reads the nonce-bound safe request from stdin and returns exactly a
V2 response on success. It rejects stale/future requests, identity/model/effort
or tool-policy drift, missing capacity, unknown subscription status, unknown or
enabled additional spending, and full input that does not fit. A failed probe
exits nonzero with a constant classification and no provider body. Normal
admission evidence retention still belongs to `invoke_validated_admission`.

### Provider status and credential boundary

| Route | Fresh read-only sources | Deliberate rejection cases |
| --- | --- | --- |
| Codex | Initialized native app-server `account/read` with `refreshToken:false`, `account/rateLimits/read`, `model/list` | Non-ChatGPT/unknown plan; unavailable included-usage permission; missing backend account ID; unknown/enabled credits; missing selected quota bucket or model/effort |
| Claude | Native `auth status --json`; authenticated OAuth profile and usage using the same explicit bearer | Unsupported or inactive personal Max profile; profile/native account mismatch; extra usage not explicitly false; missing, ambiguous or exhausted global/Fable quota |
| Antigravity | Google userinfo; same-bearer `loadCodeAssist`, `fetchAvailableModels`, `retrieveUserQuota` | Missing subject/project; missing current paid tier; onboarding tiers alone; absent/exhausted exact-model quota |

Codex requires an explicitly provisioned `CODEX_HOME`. Use the evaluator's fresh
isolated native home containing only the existing selected account's auth and
reviewed controls, never an ordinary home containing user customizations.
Native status is a subprocess; its dependency files and configuration must be
covered by the reviewed `runtime_files` list. Account identity comes from the
backend usage account ID, not the auth-file label. Current `ordinaryUsageAllowed`
must be true; percentages do not override an absent/false backend permission.
The exact selected model-to-quota-bucket mapping must also have reviewed proof.
A quota check is an observation, not a guarantee that future capacity is reserved.

Claude and Antigravity require `ADMISSION_BEARER_TOKEN` to be passed explicitly
through the command spec's `env_names`. Claude also requires `USER` for its
existing native Keychain principal lookup; this does not enable ambient API keys. The operator supplies the existing token
from the *same native account provisioning used by the adapter*. Do not put it
in argv, JSON, an evidence artifact or a committed file. The probe never reads a
credential store, changes accounts, refreshes or persists credentials. Claude
native status also needs its existing native authentication context; profile
account UUID and organization UUID are hashed after correlation with native
email/org identity. Antigravity hashes Google's returned subject plus the
same-bearer observed project ID and canonical provider/issuer. Email and decoded
unsigned JWT claims cannot replace verified Google subject identity.

HTTP is restricted to fixed HTTPS status endpoints. Environment proxies and
redirects are disabled, response size and socket time are bounded, and nonzero
cache `Age` is rejected. No inference, token-refresh or onboarding endpoint is
allowlisted. Native subprocesses have bounded time/output and process-group
cleanup; API-key and endpoint-override environment variables are excluded.
The native executable remains a reviewed trust boundary: its own automatic
auth/network behavior must be inspected, and expired provisioning may fail.

Protocol prior art was checked against the installed Codex generated app-server
schemas and the pinned [Claude OAuth status fetcher](https://github.com/steipete/CodexBar/blob/518743e2d73/Sources/CodexBarCore/Providers/Claude/ClaudeOAuth/ClaudeOAuthUsageFetcher.swift)
and [Antigravity status fetcher](https://github.com/steipete/CodexBar/blob/518743e2d73/Sources/CodexBarCore/Providers/Antigravity/AntigravityRemoteUsageFetcher.swift).
The implementation does not depend on or copy those integrations. Google's
[userinfo endpoint](https://accounts.google.com/.well-known/openid-configuration)
only proves identity; it does not prove entitlement, capacity or billing controls.

### Claude current entitlement and family quota

The collector currently supports the observed personal Max 5x route for exact
`claude-fable-5-1`: native subscription type `max`, fresh organization status
`active`, billing type `stripe_subscription`, and rate-limit tier
`default_claude_max_5x`. Other tiers and plans remain unsupported until their
current entitlement and billing semantics are reviewed. Cached login alone
cannot establish an active subscription.

[Fable plan documentation](https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan)
places Fable 5.1 within the personal Max Fable allowance. The observed provider
quota scope `{model: {id: null, display_name: "Fable"}, surface: null}` therefore
maps to this selected model's family allowance. The parser also accepts its
exact model ID with an absent or compatible family name. Missing family proof,
conflicting IDs/names, malformed scopes, and unknown scope applicability fail
closed. All family windows must have remaining quota, including entries whose
UI `is_active` flag is false. Global five-hour and weekly windows must also
remain available. Explicit `extra_usage.is_enabled: false` and same-account,
same-organization correlation are still mandatory.

### Claude exact-model refusal controls

The Claude adapter sets `switchModelsOnFlag: false`, pins `availableModels` to
its exact configured model, and explicitly sets
`CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1` in the child environment. A native
classifier refusal remains a failed attempt; it never becomes a successful
receipt by substituting a model. The runtime guard complements the settings
because managed settings can take precedence over command-line settings.
See the native [automatic fallback](https://code.claude.com/docs/en/model-config#automatic-model-fallback)
and [switching controls](https://code.claude.com/docs/en/model-config#ask-before-switching).

Bind installed-runtime source inspection and all six synthetic refusal controls
(low/medium/high × closed-book/Sources) to the support artifact hashes. Each
control must record the runtime and adapter hashes, actual request models,
request count, and preserved terminal failure. Run controls with synthetic
local transport, never a candidate inference request. These receipts prove the
observed refusal path only: they do not establish subscription capacity or
exclude unrelated billing routes. If effective policy defeats the controls or
its behavior cannot be established, leave admission unready.

### Frozen input and support evidence

The input is a declared operator configuration, not a new public admission
schema. It contains:

- `provider`: `codex`, `claude` or `agy`; exact `model` and `effort`; absolute
  resolved `binary`; `runtime_files`: nonempty `{path, byte_sha256}` entries
  covering that binary, wrappers, adapter/control code and nonsecret runtime
  configuration. Symlinks and changed bytes fail. These hashes are verified
  before and after status reads. Codex also specifies `quota_key`.
- `pricing`, `entitlement`, `capability`: the exact frozen **state** objects
  already required by admission-v2. Entitlement is `verified_subscription`,
  `verification: live_subscription`, `zero_incremental: true`, `valid_until:
  null`, and the verified stable `account_sha256`. These collectors do not
  expose an expiry date; a fabricated date is rejected.
- `support`: `provider`, `model`, `effort`, `conditions`,
  `runtime_files_sha256` (canonical digest of the exact list),
  `capability_sha256`, `pricing_sha256`, and nonempty `artifacts` entries with
  `{name, byte_sha256}`. Names are basenames of declared, snapshotted review
  evidence files. Codex additionally binds `quota_key` and
  `model_quota_mapping_verified: true`.
- The support record requires explicit reviewed conclusions:
  `current_subscription_endpoint_verified`,
  `all_additional_charge_paths_excluded`, `api_credentials_excluded`,
  `native_control_enforcement_verified`, `capacity_source_verified`, and
  `byte_token_upper_bound_verified`, all boolean true. `framing_tokens` is a
  positive reviewed upper bound for all native/system/schema framing;
  `permitted_history_tokens` is a nonnegative bound covering the allowed tool
  history for the frozen policy and limits.

These declarations are **not proof by themselves**. The independent review
must inspect the referenced bytes and demonstrate that the exact model/runtime
supports the integers, the byte-based token bound is conservative, and the
actual native adapter enforces exclusion of **every** extra-charge path.
A `useG1Credits:false` setting or API-key-free environment alone is insufficient.
Do not create a document setting the conclusions true merely to obtain a
passing result. If source or behavior evidence is unavailable, leave the route
unready and report the missing fact. Binding reviewed declarative evidence is
within the existing trusted-command contract; it is not a certificate scheme.

Supported capacity must come from reviewed model/runtime evidence, never from
requested segment limits. Effective output enforcement and effective effort may
remain unknown in the existing adapter receipts. Fit uses packet UTF-8 bytes
plus reviewed framing and allowed-history bounds, comparing the sum with both
supported input capacity and the segment's input reservation. It does not call
that an exact tokenizer. Zero incremental cost is emitted only after fresh
subscription checks and the reviewed complete no-additional-charge controls;
the ordinary pricing arithmetic still supplies the conservative segment quote.

### Safe status diagnostics before assembling admission

A bounded Codex diagnostic can run before capacity evidence is complete:

```sh
.venv/bin/python tools/admission/native_probe.py /absolute/private/status-input.json --inspect-codex
```

This input needs only `provider`, `model`, `binary` and `runtime_files`. Existing
isolated `CODEX_HOME` must already be provisioned. It prints safe plan/quota/model
fields and `admission:false`. On failure it reports only RPC direction,
allowlisted method names, integer IDs/error codes and safe account-read flags;
no RPC params, error messages, account IDs or secrets are returned. A successful
diagnostic does not establish supported capacity or complete fallback exclusion.

For Claude or Antigravity, use `--inspect-subscription` with the same minimal
input and the explicitly supplied same-native-account bearer environment. Output
contains identity-presence/correlation and status booleans only. Diagnostics do
not produce an admission receipt and cannot authorize study execution. Once
reviewed evidence is assembled, omit the diagnostic flag and invoke through the
normal hash-bound admission-command/controller path. Refresh admission with a
new nonce at launch and every required segment; saved diagnostics never become
permanent readiness.
