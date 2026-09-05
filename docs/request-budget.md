# Request-level budget control

`RequestBudgetController` is the pre-request control for every `metered` or
`existing_credit` research route. A route cannot enter candidate execution
unless its experiment-manifest entry freezes a non-null
`request_budget_mechanism_sha256` and the runtime supplies the matching trusted
integration. A `verified_subscription` route may freeze a mechanism when its
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

For existing credit, the full maximum segment charge is committed in evidence
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

## Runtime map

`run-research --request-budgets request-budgets.json` loads an explicit route
map. The map and every referenced file should remain private:

```json
{
  "schema": "ukrainian-llm-eval.research-request-budgets.v1",
  "routes": {"my-metered-route": "request-budgets/my-metered-route.json"}
}
```

Each route file has schema
`ukrainian-llm-eval.request-budget-route.v1` and exactly `mechanism` plus
`counter_command` objects. Commands are supplied by the operator and are never
discovered from benchmark, manifest or model output.
