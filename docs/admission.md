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
these five source files from the reviewed checkout when assembling a command
spec; they are operator tools, not dependencies of the installed evaluator.
Declare `native_probe.py` as the script, the other four Python files as
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
| Antigravity | Same-token Google userinfo; fresh owned native `RetrieveUserQuotaSummary` and `GetUserStatus` | Unverified principal; unsupported current user tier; wrong exact model/enum; missing or exhausted model/group quotas |

Codex requires an explicitly provisioned `CODEX_HOME`. Use the evaluator's fresh
isolated native home containing only the existing selected account's auth and
reviewed controls, never an ordinary home containing user customizations.
Native status is a subprocess; its dependency files and configuration must be
covered by the reviewed `runtime_files` list. Account identity comes from the
backend usage account ID, not the auth-file label. Current `ordinaryUsageAllowed`
must be true; percentages do not override an absent/false backend permission.
The exact selected model-to-quota-bucket mapping must also have reviewed proof.
The ordinary `codex` bucket is always checked, together with every additional
bucket applicable to the selected model. The collector conservatively matches
case-folded model slugs and catalog-backed display names, including
`normalModelSlug`; this extension is stricter than the native TUI's exact
`limitName` comparison. Conflicting or unmapped extra identities fail closed.
Only explicit catalog bindings can exclude another model's extra bucket; an
unknown extra bucket is never silently ignored.
A quota check is an observation, not a guarantee that future capacity is reserved.

Claude and Antigravity require `ADMISSION_BEARER_TOKEN` to be passed explicitly
through the command spec's `env_names`. Claude also requires `USER` for its
existing native Keychain principal lookup; this does not enable ambient API keys. The operator supplies the existing token
from the *same native account provisioning used by the adapter*. Do not put it
in argv, JSON, an evidence artifact or a committed file. The probe never reads a
unselected credential store, changes accounts, or requests token refresh. Claude
native status also needs its existing native authentication context; profile
account UUID and organization UUID are hashed after correlation with native
email/org identity. Antigravity hashes Google's verified returned subject plus
the canonical provider/issuer. Email and decoded
unsigned JWT claims cannot replace verified Google subject identity.

Antigravity additionally requires the explicit private `native_home` in frozen
input. Only its known `.gemini/antigravity-cli/antigravity-oauth-token` file is
read, with ownership/permission checks and exact access-token equality to the
selected bearer. Its supported consumer credential shape is copied into a fresh
private home after removing `refresh_token`; expiry must exceed the complete
collector deadline plus margin. The original is preserved. No ambient plan
cache, sessions, settings, environment API credentials or proxies are imported.
Bind installed evidence that this access-token-only shape works and no fallback
auth/refresh source is consulted; post-run hash checks alone do not prove that.

Each validated request precedes a new owned native bootstrap with no prompt.
This fresh, cache-free bootstrap supplies current-plan evidence; an RPC timestamp
alone does not establish plan freshness or subscription expiry. Only the live
child's verified loopback listeners are considered, preserving IPv4/IPv6 family.
The collector sends HTTPS JSON/Connect status calls, quota `forceRefresh: true`
first, with bounded time/output and cleanup/reaping on failure. Self-signed TLS
is accepted solely for the owned loopback endpoint. Native runtime and both
original/copied credential bytes are checked for drift. Include the installed
native executable, `/usr/sbin/lsof`, `/usr/bin/git` and collector sources in the
reviewed runtime dependencies.

The supported current `userTier.id` is `g1-ultra-lite-tier`; legacy `planInfo`
labels cannot override it. Exact reviewed Gemini 3.8 Flash low/medium/high
aliases must match fresh model enums M320/M319/M318 (provider prefix
`MODEL_PLACEHOLDER_`) and their exact labels. Selected model quota and both
`gemini-weekly` and `gemini-5h` group windows must remain positive. These checks
do not establish separate capacity or no-additional-charge support conclusions.
The obsolete remote Code Assist probes are removed from the endpoint allowlist.

HTTP is restricted to fixed HTTPS status endpoints. Environment proxies and
redirects are disabled, response size and socket time are bounded, and nonzero
cache `Age` is rejected. No inference, token-refresh or onboarding endpoint is
allowlisted. Native subprocesses have bounded time/output and process-group
cleanup; API-key and endpoint-override environment variables are excluded.
The native executable remains a reviewed trust boundary: its own automatic
auth/network behavior must be inspected, and expired provisioning may fail.

Protocol prior art was checked against the installed Codex generated app-server
schemas and the pinned [Claude OAuth status fetcher](https://github.com/steipete/CodexBar/blob/518743e2d73/Sources/CodexBarCore/Providers/Claude/ClaudeOAuth/ClaudeOAuthUsageFetcher.swift)
and [Antigravity native status probe](https://github.com/steipete/CodexBar/blob/518743e2d73/Sources/CodexBarCore/Providers/Antigravity/AntigravityStatusProbe.swift).
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
  `native_control_enforcement_verified`, `capacity_source_verified`,
  `byte_token_upper_bound_verified` and `initial_history_verified`,
  all boolean true and backed by the frozen
  reviewed artifacts. `framing_tokens` is a positive reviewed upper bound for
  all native/system/developer/tool-schema/setup framing;
  `initial_history_tokens` is a nonnegative bound covering only history actually
  included in the initial request, including restored, preloaded or setup
  messages. Explicit reviewed evidence must substantiate the bound; zero does
  not follow merely from a fresh process. The previous private
  `permitted_history_tokens` field is rejected rather than reinterpreted.
- `input_capacity_basis` is an explicit private discriminator. For
  `combined_window`, `context_window_tokens` is the source-reviewed combined
  input/output window and `output_headroom_verified` must be true;
  `output_headroom_tokens` is positive, source-reviewed safe output headroom.
  It must cover at least capability `max_output_tokens`, but that inequality
  alone is not proof. When native output enforcement is unknown, reserve the
  actual runtime/model maximum output allowance or otherwise proven safe
  headroom, never merely the requested suite output cap. Available input must
  satisfy `0 < context_input_tokens <= context_window_tokens - output_headroom_tokens`.
- For `input_capacity_basis: native_usable_input`, bind original native
  `context_window_tokens`, integer `effective_context_window_percent` (1–100),
  and `native_usable_input_verified: true` to exact model/runtime source evidence.
  That source must define usable input after native system/tool/output reserves.
  The bound is `floor(context_window_tokens * effective_context_window_percent / 100)`;
  capability input cannot exceed it. Do not subtract output again or invent a
  separate numeric headroom. This variant forbids both `output_headroom_*`
  fields; combined-window evidence forbids the native percentage/proof fields.
  Missing or mixed interpretations fail closed. For example, a verified native
  entry of 272,000 at 95% yields 258,400 usable input tokens. This says nothing
  about a separate maximum-output guarantee or effective output enforcement;
  supported-output checks remain separate. Do not relabel unrelated net-input
  documentation as a combined window or as this native percentage interpretation.


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
remain unknown in the existing adapter receipts. The native `input_fits`
observation concerns the complete initial request.
It uses the complete prompt UTF-8 byte upper bound plus reviewed native, system,
developer, tool/schema and setup framing, and actual included initial history.
These components must not overlap. The sum is compared with supported available
input capacity and the segment's input reservation. `context_input_tokens` is
available input after verified output headroom has already been reserved;
output is not subtracted again during this comparison. Requested context or
output settings are not evidence of supported capacity. This is a conservative
bound, not an exact tokenizer count. Later tool-context failures remain retained
failures; initial fit guarantees neither compaction nor successful completion.
Tool limits, budget enforcement and public metrics are unchanged. Zero incremental cost is emitted only after fresh
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

## Personal metered Gemma through native OpenCode

`tools/admission/gemma_probe.py` implements admission-result.v1 for the selected
`google/gemma-4-31b-it` OpenRouter route, exact `venice/bf16` backend and `Venice`
provider. It supports the native reasoning off/on configurations with null
`effort`. It does not launch OpenCode or send completions, and cannot turn a
successful diagnostic canary into admission. The selected backend's current
published limit must cover every requested suite limit; a published 8192-token
maximum rejects a 16384-token request without reducing that request or changing
backend.

The probe uses three bounded, current read-only GETs with verified TLS, no
redirects or environment proxies, and no credential refresh:

- `/api/v1/key` on `openrouter.ai` authenticates the exact selected ordinary key;
- `/api/v1/credits` on that host obtains `total_credits` and `total_usage` using
  the same key; permission denial rejects admission without seeking a
  management key;
- `/api/v1/models/google/gemma-4-31b-it/endpoints` checks the exact backend,
  model, availability, context/output capacity, supported controls and prices.
  This public request receives no credential.

The frozen `credential_sha256` is SHA-256 of the exact UTF-8 token, and
`key_env` names its sole environment input. Neither token nor fingerprint is
used as an account identity. For a reviewed **personal** key,
`account_sha256` is the canonical digest of
`{"provider":"openrouter","creator_user_id":<authenticated subject>}`.
It identifies the returned provider user, not an organization billing pool.
The probe rejects management/provisioning/free-tier keys, unknown identity and
other billing kinds. This personal metered interpretation must not be reused
for organization accounts or existing-credit aggregation.

V1 `valid_until` is a restrictive latest-use bound no later than the actual
provider-reported key expiry and any earlier known access expiry. A missing or
expired key expiry rejects admission. A future expiry does not establish
current eligibility: current funds, finite key limits, route health and all
other gates must also pass. It is not a subscription-expiry claim.

The probe requires available provider funds (and `limit_remaining` when a
finite key limit applies) to cover the full frozen
`maximum_segment_micro_usd` plus **all** unresolved shared new-spend
commitments. Including old commitments with unknown account identities is
conservative. Decimal monetary inputs are retained exactly and available
funds are rounded down to integer micro-USD. The independent shared cap must
also cover the next full reservation. Funds below the entire experiment cap
are not automatically insufficient. Because this is a metered route,
`credit_available_micro_usd` remains null in the admission result; no provider
balance is printed in its output.

Supply a private frozen configuration with these groups:

| Group | Required inputs |
| --- | --- |
| Route | `provider: "openrouter"`, the exact `model`, `effort: null`, `backend: "venice/bf16"`, `expected_provider_name: "Venice"`, boolean `reasoning_enabled`, `account_scope: "personal_provider_user"` |
| Credential | `key_env`, `credential_sha256`; no token in the file |
| Native runtime | `binary` and `runtime_files` with absolute paths and byte hashes, as for the native subscription probe |
| Frozen states | V1 `pricing`, `entitlement`, and `capability` objects with the exact fields accepted by `validate_admission_result`; the full `maximum_segment_micro_usd` |
| Shared ledger | `ledger: {"path": <existing absolute path>, "ledger_id": <frozen ID>, "cap_micro_usd": <frozen cap>}` |
| Public package | `budget_wheel: {"name": <wheel basename>, "byte_sha256": <wheel hash>}` |
| Reviewed support | The fields below, with each artifact also declared to the command runner |

`support` binds `runtime_files_sha256`, `pricing_sha256`,
`capability_sha256`, and the same `provider`, `model`, `backend`,
`reasoning_enabled`, `account_scope`, and `credential_sha256`. Its nonempty
`artifacts` list contains basename/byte-hash pairs. It lists the permitted
`conditions`, positive `framing_tokens`, nonnegative `initial_history_tokens`,
positive `context_window_tokens`, and positive `output_headroom_tokens`.
`initial_history_verified` and `output_headroom_verified` must both be boolean
true. The initial request includes packet UTF-8 bytes (a conservative token
upper bound only when independently verified), native/system/developer framing,
tool schemas, setup messages, and any restored or preloaded history. Zero
initial history requires explicit evidence; a fresh process alone is insufficient.
The legacy `permitted_history_tokens` field is rejected.

`context_window_tokens` is the reviewed combined input/output window and must
match the selected provider's live window; the reviewed native runtime must
support that window or the net input capability must reserve the same output
headroom against its lower safe ceiling.
`output_headroom_tokens` covers the source-reviewed maximum runtime output,
including unknown effective output through a proven safe maximum, and must be
at least the capability's output maximum. `capability.context_input_tokens` is
already net of this headroom and cannot exceed the combined window minus
headroom. The fit check compares complete initial input against this net capacity
and the cumulative input budget, without subtracting output twice. A published
context size by itself is not input-fit proof. Later tool requests remain subject
to the existing per-request and cumulative budgets. Changed private support and
capability files require refreshed hashes and reviewed evidence.

The following support conclusions must all be true and backed by the declared
reviewed artifacts: `personal_key_ownership_verified`,
`same_credential_native_execution_verified`,
`native_control_enforcement_verified`,
`provider_routing_and_price_caps_verified`,
`byte_token_upper_bound_verified`, and `all_non_token_fees_excluded`.
These flags validate the linkage to reviewed proof; setting them to true does
not manufacture that proof. Missing framing/fee evidence remains a blocker.
The native configuration and its provider price ceilings must be covered by
those artifacts. Requested reasoning is route-bound, but this probe does not
claim observed effective reasoning.

Declare the probe as the `script`, the interpreter as `executable`, the private
config as `runtime_lock`, and `probe_common.py`, the pinned public-package
wheel and every support artifact as `dependency` files in the existing
admission command specification. Its argv is the absolute interpreter, probe
and config paths. Use only the declared credential environment name and an
adequate bounded command timeout for three 15-second HTTP operations. The
runner remaps the config argument into its private file snapshot. Wheel and
support names are resolved relative to that snapshotted config. The probe
checks the wheel's bytes before adding that exact snapshot archive to its
import path, then calls the existing
`SharedSpendingLedger.inspect_readiness` API. It rejects an ambient package
import, a missing/drifted ledger or dependency, and unsupported ledger state;
there is no alternate SQL implementation or ledger initialization.

The route must bind canonical pricing/entitlement/capability **state** hashes,
the command identity, request-budget mechanism and exact separate operator
authorization. Raw official-document hashes belong in the corresponding
mechanism/support evidence rather than being substituted for canonical
admission state hashes. Keep the existing shared spending policy and ledger
path across canaries and execution. The probe does not reserve, settle,
reconcile or allocate candidate attempts. Admission is a time-bound
observation; execution must still refresh admission and atomically reserve
before each segment.
