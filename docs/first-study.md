# First study preparation

The [protocol](../benchmarks/first-study-protocol.json) records the selected
configuration matrix and concrete common limits for review. It is a preparation
input, not a `plan-research` specification or an admission certificate. It
contains no invented pricing, entitlement, provider-probe or authorization digests.

There are eleven configurations: Astra, Fable and Gemini at low/medium/high,
and Gemma with reasoning off/on. Each has both conditions and three repeats on
each complete suite: **198 cells and 35,178 segment sessions**. Tool rounds and
native auxiliary requests can produce additional model requests; this session
count is not a request count or cost estimate.

| Suite | Full denominator | Sessions per cell | Timeout per session | Requested output tokens | Reference-call cap |
| --- | --- | --- | --- | --- | --- |
| NMT 2022 demonstration language block | 20 tasks / 35 points | 20 | 300 seconds | 4,096 | 20 |
| ULP | 347 questions | 347 | 300 seconds | 4,096 | 20 |
| UA-GEC public test | 2,696 sentences / 166 documents | 166 | 600 seconds | 16,384 | 20 |

Limits are common within a suite. They are concrete review inputs, not claims
that every native provider enforces the requested output cap. The AGY adapter
reports its effective output cap as unknown. Admission must account for actual
native limits and complete tool history; a small synthetic answer or prompt
byte length does not prove provider token fit for the largest document.
Requested effort is not equal computation across providers, and unreported
effective model/effort stays unknown.

Use the existing source profiles and preparation receipts described in
[benchmark preparation](benchmarks.md). Reconstruct each full packet and key
with `verify-benchmark`, derive its segment plan, and freeze the protocol,
packet, key and segment hashes. NMT matching rows remain atomic; GEC document
membership comes from the original M2. Store keys and original annotated
sources separately from candidate-visible packets. Runtime maps have no key field.

The prepared matrix is an inventory of intended cells. The scheduler's
`build_execution_plan` remains authoritative for execution order, attempt IDs
and reservations. Assemble its specification only after binding the reviewed
implementation, scorer, exact route configurations, trusted admission commands,
operator authorizations and request-budget mechanisms. Retain all cells under
the existing sequential shared $10 cap, including earlier canary commitments;
do not create a fresh ledger to replenish the budget.

Native live-subscription admission may explicitly use the separate V2 result
form in [admission](admission.md). The integration must freshly establish active
status and disabled paid fallback. A null provider expiry is disclosed, not
replaced with a token expiry or quota reset. Passing V2 fixture tests does not
establish that a real provider command supplies those observations.

Before launch, require the exact reviewed source revision, green CI, current
native control/canary evidence, complete live admission maps, and an executable
pinned scorer runtime. Source or native-runtime drift requires renewed checks.
The whole-codebase review and verdict identify the exact revision; subsequent
changes require review of the changed revision.

The pinned GEC runtime is Linux amd64. Apple Silicon execution requires working
amd64 emulation or a verified Linux scoring host. Image inspection alone is
insufficient: execute the scorer and parity checks. Do not alter the scorer or
substitute a metric to accommodate a host that cannot execute that architecture.

Before any scored request, run the admission-only `check-research` command
documented in [research execution](research-execution.md). For the complete
matrix, its 198 fresh representative probes cover 35,178 structurally validated
segment bindings. It preserves failed probes and inspects the existing shared
ledger without allocating scored attempts or reservations. Those observations
expire with their underlying evidence; they are not execution authorization,
and each launched segment must refresh admission. Stop after this observation
when the operator has authorized launch preparation only.

When scored execution is separately authorized, use [run-research](research-execution.md), preserve every attempt,
and score complete cells using the separate offline custody map. The
[results document](results.md) tracks coverage, failures and paired results.
The [release procedure](releasing.md) still requires tested public artifacts
and public installation verification after the experiment.
