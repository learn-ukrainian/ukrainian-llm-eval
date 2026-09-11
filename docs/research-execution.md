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
semantics. V1 uses a trusted exact counter command; V2 uses a pinned
provider-documented input upper bound; V3 adds the explicitly authorized
conservative final-usage settlement contract. See
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
Each paid route needs the evidence required by its frozen request-budget
version: V1 exact counting, V2 provider-documented bounds and authoritative
account charges, or V3 provider-documented bounds and authorized conservative
final-usage settlement. An exact tokenizer is not a universal requirement.
Requested limits and byte counts are not observed enforcement or token-fit proof.

## Admission-only launch observation

Use `check-research` to inspect the actual frozen plan before scored execution.
It accepts the same runtime, admission, authorization, budget and Sources maps
as `run-research`, plus a new private observation directory:

```sh
ukrainian-llm-eval check-research \
  --inputs runtime-inputs.json \
  --manifest experiment.json \
  --execution-plan execution-plan.json \
  --execution-root private-research-run \
  --evidence-root private-launch-observation-001 \
  --admission-specs admission-specs.json \
  --operator-authorizations authorizations.json \
  --request-budgets request-budgets.json \
  --shared-spending-ledger /absolute/private/shared-budget.sqlite \
  --sources-url-env my-route=SOURCES_MCP_URL
```

This command cannot execute candidates and has no execution or ledger-reset
switch. It validates complete maps, all segment bindings, and suite-derived
configurations before issuing one fresh nonce-bound representative probe for
each cell, including each repeat. The representative is the largest UTF-8
prompt in that cell: a conservative byte-size profile, not a token-count
guarantee. `structural_segments` counts validated bindings;
`representative_probes` counts actual admission attempts. These are distinct
denominators. Admission commands receive only counts and identities, never
question text or grading keys.

The existing shared ledger must be supplied for sequential spending. Inspection
retains prior commitments and checks the next worst-case segment capacity; it
does not demand funding for the entire matrix. No scored attempt IDs, request
budget evidence, or reservations are allocated. Existing execution files and
ledger state are read without interrupted-attempt recovery. A retained stop is
a failure, not an instruction to reset the run. Unsupported legacy evidence
inspection fails explicitly.

Immutable observation and admission receipts use the existing evidence store
under the new observation directory. Failed probes remain, and remaining cells
are still checked. Exit 0 means all representative observations passed at the
recorded times; exit 2 means invalid inputs or failed observations.
`execution_admitted` is always false: this is no permanent admission certificate.
`run-research` still requires fresh admission at each segment, and a later
budget-cap stop remains possible. Preserve observations privately and review
them before sharing.
