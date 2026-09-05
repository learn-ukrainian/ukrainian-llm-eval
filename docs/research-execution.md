# Running a frozen research plan

`run-research` is the live execution command for a previously frozen
experiment manifest and execution plan. It is deliberately separate from
`plan-research` and `score-research`: planning makes no provider calls, and
scoring is an offline key-custodian operation.

The command receives a candidate-visible runtime map with exactly these
required fields:

```json
{
  "schema": "ukrainian-llm-eval.research-runtime-inputs.v1",
  "packets": {"ulp": "packets/ulp.json"},
  "segment_plans": {"ulp": "segments/ulp.json"},
  "configs": {"my-route": "configs/my-route.json"}
}
```

The map contains no grading keys, references, or prior responses. Relative
file references resolve beside the runtime map. A private `sources_urls` map
is optional, but a separate Sources route map or environment assignment is
preferable so endpoint values do not end up in a checked-in input file. The
runtime map is an operator input and is not written into the candidate
evidence archive.

Admission commands and operator authorization are separate, explicit maps.
They are never discovered from benchmark packets, manifests, or result files.
Each map resolves route-specific JSON files relative to that map:

```json
{
  "schema": "ukrainian-llm-eval.research-admission-specs.v1",
  "routes": {"my-route": "trusted-admission-command.json"}
}
```

```json
{
  "schema": "ukrainian-llm-eval.research-operator-authorizations.v1",
  "routes": {"my-route": "operator-authorization.json"}
}
```

An admission command is a trusted local integration, not a sandbox for
untrusted code. Its declared interpreter/script/dependencies are checked by
the admission command contract and the command identity must match the route
hash frozen in the manifest. The authorization file separately states whether
incremental new spending is allowed and its maximum amount. The exact route-bound
record authorizes route execution; `allow_paid: false` permits an explicitly
selected subscription or existing-credit route while forbidding new charges.
Matching hashes
alone do not prove provider health, entitlement, pricing, or model/effort
support; every segment still requires a fresh nonce-bound admission result.

Metered and existing-credit routes additionally require an explicit
request-budget map. Its route files freeze the provider counting and output
semantics plus a trusted local counter command. See
[request-level budget control](request-budget.md). A verified-subscription
route may omit the mechanism by freezing `request_budget_mechanism_sha256` as
`null`; this discloses that exact request-level cost proof is unavailable.

```json
{
  "schema": "ukrainian-llm-eval.research-request-budgets.v1",
  "routes": {"my-route": "request-budgets/my-route.json"}
}
```

Run a plan with private evidence storage as follows:

```sh
ukrainian-llm-eval run-research \
  --inputs runtime-inputs.json \
  --manifest experiment.json \
  --execution-plan execution-plan.json \
  --execution-root private-research-run \
  --admission-specs admission-specs.json \
  --operator-authorizations authorizations.json \
  --request-budgets request-budgets.json \
  --sources-url-env my-route=SOURCES_MCP_URL
```

The Sources environment variable is read by the process and its value is not
printed. The alternate `--sources-urls sources.json` input accepts either a
route-to-URL object or this strict form:

```json
{
  "schema": "ukrainian-llm-eval.research-sources-inputs.v1",
  "urls": {"my-route": "env:SOURCES_MCP_URL"}
}
```

Use `--resume` with the same frozen inputs to continue unstarted independent
cells. A started segment is never retried; an incomplete attempt is finalized
as interrupted and remains in the evidence store. The scheduler stops on an
admission failure, observed identity or budget drift, invalid segment output,
or another frozen stop condition. Raw candidate and admission evidence stays
under the owner-only execution root. Review it before sharing any aggregate.

The command prints one JSON progress record per completed cell or durable stop
and returns exit code 0 only when every visited cell completed successfully.
Exit code 2 means invalid input, a stopped experiment, or a failed cell; the
private evidence and any saved stop record remain the authoritative account.

This command is an implementation interface and does not by itself establish
that a provider route is eligible for the public experiment. Before a public
run, verify the exact model/effort inventory, live route claims, spending
authorization, installed behavior, independent review, and release gates.
Request-level enforcement is implemented, but no bundled mechanism claims a
real provider tokenizer or framing contract. Each inaugural paid route still
needs a provider-specific counter and semantics receipt before the CLI can be
used to claim that route is ready.
