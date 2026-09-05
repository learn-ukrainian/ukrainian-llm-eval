# Request-level budget control

`RequestBudgetController` is the pre-request control for every `metered` or
`existing_credit` research route. A route cannot enter candidate execution
unless its experiment-manifest entry freezes a non-null
`request_budget_mechanism_sha256` and the runtime supplies the matching trusted
integration. Version 1 retains the exact provider counter and upfront
full-schedule admission. Version 2 uses a documented provider context upper
bound and a shared sequential spending ledger. Version 3 can settle that
reservation to a conservative final-usage-derived monetary upper bound when
the provider has authoritative final usage but no inline dollar charge.
Receipts from one version are never interpreted as another. A
`verified_subscription` route may freeze a v1 mechanism when its
HTTP provider supports exact accounting, or use `null` to disclose that this
stronger cost proof is unavailable. That disclosure must not be reported as an
equivalent token or cost guarantee.

## Frozen mechanism

The strict schema `ukrainian-llm-eval.request-budget-mechanism.v1` contains:

- the route and provider identities;
- `canonical-json-utf8-newline-v1` serialization;
- the verified local counter-command and provider counter-semantics SHA-256s;
- `provider-native-full-request-v1` input semantics;
- the provider's output-parameter name and attestations that it limits
  reasoning, tool calls and final output together;
- exact paths for provider-reported input and reasoning-inclusive output usage;
- an optional reported-cost path classified as an account charge or a
  nonincremental subscription estimate, with per-request aggregation semantics;
- `highest-applicable-input-rate` cache billing, which charges every counted
  input token at the frozen full input rate rather than assuming a discount;
- a positive maximum UTF-8 byte size for each Sources tool result.

The implementation does not supply a universal tokenizer or infer provider
framing. The trusted counter must implement the frozen provider semantics. Its
bounded command spec uses the same `python-script-v1` substrate as admission,
must have no environment inputs, and receives only the candidate-visible JSON
request. It receives no benchmark key, reference answer, provider credential
or admission state. A test command that counts bytes establishes controller
behavior only and is not evidence that a real provider tokenizer is ready.

The counter returns exactly:

```json
{
  "schema": "ukrainian-llm-eval.request-token-count.v1",
  "request_sha256": "<sha256-of-exact-stdin-and-http-body>",
  "counter_semantics_sha256": "<frozen-provider-semantics-sha256>",
  "input_tokens": 1234
}
```

The controller serializes the request body with a trailing newline, checks the
counter result, commits the count and cost to private append-only evidence, and
returns those exact bytes to the HTTP adapter. The body includes model and
effort fields, messages, tool schemas, response schema, assistant tool calls
and arguments, and serialized tool results from prior rounds. Provider framing
outside the HTTP body is part of the counter's frozen semantics and must be
included by that provider-specific counter.

## Documented provider upper bounds

The strict `ukrainian-llm-eval.request-budget-mechanism.v2` schema is an
explicit alternative for paid APIs whose official interface does not expose an
exact pre-send tokenizer for complete provider framing. It records the
documented maximum accepted input tokens per request, maximum requests per
segment, coverage of hidden provider framing, and the evidence SHA-256. It also
binds the reasoning-inclusive output parameter and maximum, pricing evidence,
concrete backend-identity evidence, and either authoritative per-request
account-charge usage paths or an explicit all-null declaration that no inline
authoritative charge is available.

This mechanism labels its input commitment as
`provider_context_upper_bound`; it does not claim exact tokenization. Before
each request it commits the complete documented context maximum. The route
billing totals must cover every permitted request and output, including tool
rounds, full-rate cache treatment, fees, and integer rounding. Missing final
account-charge evidence leaves the full segment reservation unresolved.

## Conservative final-usage settlement

The strict `ukrainian-llm-eval.request-budget-mechanism.v3` schema extends the
v2 provider bounds without changing v1 or v2 receipts. Its cost path, kind, and
scope must all be `null`. A manifest using it must select
`ukrainian-llm-eval.spending-policy.v2` with settlement
`authoritative_account_charge_or_conservative_final_usage_upper_bound`; the v1
policy continues to authorize account-charge settlement only. Version 3 also
freezes:

- the maximum number of billable requests in a segment;
- per-request, per-component upward rounding and its micro-USD quantum;
- an aggregate maximum for all additional request, routing, minimum, fixed,
  and similar non-token fees in the segment, with evidence that zero is the
  maximum when no such fee applies;
- evidence identities for final usage semantics and additional fees; and
- attestations that the input and output rates are the maximum applicable
  rates for the pinned backend, usage is per-request and final, input usage
  covers all billable categories, and transport performs no automatic paid
  retry.

Each observed request contributes separate upward-rounded input and
reasoning-inclusive output bounds plus its frozen tool charge. After every sent
request has valid final usage, the reservation may settle to:

```text
U = sum(per-request rounded input + rounded output + tool charge)
    + maximum additional segment fees
```

The controller verifies `U <= W`, where `W` is the whole-segment reservation.
The ordered request identities, usage, component bounds, formula inputs,
pricing, backend, output, usage-semantics, and fee evidence identities are
bound into settlement evidence. The receipt calls `U` a
`conservative_final_usage_upper_bound`; it never labels it as billed cost or an
account charge.

A missing, partial, malformed, ambiguous, or lost usage response leaves the
whole `W` unresolved, even when earlier requests in the same segment have valid
usage. The same applies if pricing can vary above the frozen rates, usage may be
revised upward, provider fallback is possible, a paid retry is hidden, or any
request or fixed fee lacks a finite bound. The evaluator does not partially
settle a segment.

## Cumulative limits and credit

Each request reserves its full reasoning-inclusive output allowance before it
is sent. The controller enforces cumulative input, output, tool-call and
completion-round limits. Provider usage is checked after each response before
any Sources call or later completion request. Missing usage, an observed count
above the commitment, or an observed charge above the frozen segment maximum
fails the attempt and stops the research route.

Formula-derived conservative charge and provider-reported charge remain
separate evidence. A native subscription estimate can be preserved for
disclosure, but is never treated as incremental spend or proof of a debit.

Sources results are canonically serialized once. A result above the frozen
byte limit is rejected without truncation; only its byte count and SHA-256 are
added to budget evidence.

For v1 existing credit, the full maximum segment charge is committed in evidence
before the first candidate request. All earlier commitments, including failed
or interrupted attempts, remain charged against later balance observations.
The implementation does not release them based on an estimate or a repeated
stale balance. Future settlement support must require authoritative evidence
that release is safe.

Budget evidence is stored privately under
`<execution-root>/request-budget-evidence/`. Completed and failed candidate
identity records bind the corresponding verified budget-receipt SHA-256. On
resume, the schedule lock permits an incomplete budget to be finalized as
interrupted; its credit commitment remains charged. A budget created before a
crash but lacking its candidate receipt makes that candidate permanently
ineligible for retry.
On resume, this orphan commitment stops the entire experiment with a durable
`admission_failed` result. Remaining routes and cells do not run from that
execution root; the commitment and incomplete experiment remain evidence.

Versions 2 and 3 also require a private absolute runtime path to one shared SQLite ledger. The
portable manifest contains only the logical ledger ID, cap, reservation scope,
settlement rule, and cap-stop outcome. The ledger lives outside every execution
root and is shared by canaries and scored runs. On first use it binds the exact
logical ID and cap; reopening the path with a changed ID or cap fails.
The ledger does not discover other files with the same logical ID. Runtime
orchestration must pin and reuse the same absolute path for the complete
canary-and-benchmark authorization.

Under one atomic transaction, the ledger enforces
`settled_new_spend_upper_bounds + unresolved_worst_case_reservations +
next_worst_case <= cap`.
A whole segment is reserved after admission and before its first provider
request. Timeout, lost response, missing billing, interruption, and restart
retain its full worst case. A v2 reservation settles only from authoritative
final account charges for every sent request. A v3 reservation settles only
from the complete final-usage proof above. Other estimates never settle it.
Existing-credit settlement, including a v3 usage-derived upper bound, remains
retained against later balance observations until separate authoritative
reconciliation proves that the charge is reflected in the observed balance.

## Runtime map

`run-research --request-budgets request-budgets.json` loads an explicit route
map. The map and every referenced file should remain private:

```json
{
  "schema": "ukrainian-llm-eval.research-request-budgets.v1",
  "routes": {"my-metered-route": "request-budgets/my-metered-route.json"}
}
```

Each exact-counter route file has schema
`ukrainian-llm-eval.request-budget-route.v1` and exactly `mechanism` plus
`counter_command` objects. A provider-bound route file has schema
`ukrainian-llm-eval.request-budget-route.v2`; the v3 usage-bound equivalent has
schema `ukrainian-llm-eval.request-budget-route.v3`. Each has exactly its
`mechanism`; the runtime controller receives the shared ledger path separately.
Commands and evidence are supplied by the operator and are never discovered
from benchmark, manifest or model output.
