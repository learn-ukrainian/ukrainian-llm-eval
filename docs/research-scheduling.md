# Segmented research execution

Primary research uses one fresh session per complete NMT task, ULP question,
or UA-GEC document. Matching rows stay together. Each model/effort/condition
cell must cover the whole frozen suite before receiving a score. Whole-packet
`run` and `pair` are separate endurance diagnostics.

Preparation, planning, live execution and offline scoring have CLI commands.
Live execution requires explicit runtime inputs, trusted admission command
specifications, separate operator authorization files, and request-budget
mechanisms for every paid or existing-credit route. Do not substitute
the diagnostic `pair` command for a primary research experiment.
The [trusted admission contract](admission.md) describes the implemented
validation and subprocess boundary and its remaining live-provider gates.
The [request-budget contract](request-budget.md) describes exact-body counting,
cumulative limits, and retained account-level credit commitments.

## Preparation and custody

`segmentation.derive_segment_plan` accepts a gold-free packet, suite identifier,
protocol hash and expected denominator. UA-GEC preparation also reads the
original M2 to determine document membership. Its output keeps only identifiers,
counts and hashes. It does not retain annotations. Keep original M2 and keys
outside the execution environment.

`derive_segment_packet` assigns fresh local opaque IDs to each segment while
retaining the existing question/correction packet schemas. `validate_segment_plan`
checks the exact ordered partition and hashes. Validation without the original
M2 checks the frozen structure; it does not independently verify document
membership against source bytes. Perform source-aware preparation first.

`benchmark_manifest.build_experiment_manifest` binds the segment plans, source
and profile hashes, route/configuration identities, common per-suite limits,
the complete private key's canonical hash, scorer identity and pricing/entitlement evidence. Limits explicitly apply to
each segment. The generic constructor permits deliberate repeat counts; the
public experiment requires three. Primary `score-research` rejects other repeat
counts explicitly rather than returning an empty summary. Unsupported conditions need an evidence hash
explaining their absence and remain visible in the experiment definition.

`build_execution_plan` constructs every ordered cell and reservation. Conditions
alternate order across repeats. The v1 policy rejects a total reservation above
the shared new-spend ceiling, initially USD 10. The explicit v2
`sequential_shared_cap` policy preserves the complete matrix and order even when
its conservative total exceeds the cap; runtime reserves each whole segment
atomically against the shared ledger. Amounts use integer micro-USD; metered rates
are per million tokens and multiplication rounds up. Input bounds must cover
all requests, including repeated histories and tool results. Output bounds
cover all permitted tool rounds. Unknown pricing is not zero cost. Subscription
or existing-credit zero incremental cost requires separate entitlement evidence.

These pure constructors bind supplied evidence; they do not authenticate a
provider, prove entitlement, or authorize spending.

```sh
ukrainian-llm-eval prepare-segments --questions questions.json \
  --suite ulp --protocol-sha256 "$PROTOCOL_SHA256" \
  --denominator denominator.json --output segments.json
ukrainian-llm-eval plan-research --specification experiment-specification.json \
  --manifest experiment.json --execution-plan execution-plan.json
```

The denominator file is a JSON object, such as `{"items":347,"points":347}`
for the complete ULP source. GEC preparation also requires `--gec-source`.
The specification uses the named arguments of `build_experiment_manifest`,
including full segment plans. V1 planning reports `execution_admitted: false`
and writes no outputs if the complete reservation total exceeds the ceiling.

## Offline scoring CLI

`research_scoring.scorer_identity_sha256` binds the exact MCQ scorer code and,
for GEC, the immutable image ID into the experiment's scorer hash. Keys are
hashed as complete JSON objects using the canonical `core.digest`; the GEC
key's internal body hash is a different value.

```sh
ukrainian-llm-eval score-research --inputs scoring-inputs.json \
  --manifest experiment.json --execution-plan execution-plan.json \
  --execution-root private-run --scorer-bindings scorer-bindings.json \
  --scoring-evidence-dir private-scoring --output private-score-report.json
```

`scoring-inputs.json` has schema
`ukrainian-llm-eval.research-scoring-inputs.v1` and four file maps: `packets`,
`segment_plans`, and `keys` map suite IDs to paths; `configs` maps route IDs
to paths. Relative paths resolve beside this input file. This is an offline
key-custodian input, never an execution or candidate input.

Scoring checks the preserved receipt set, cell-artifact hashes, partition,
configurations and frozen key/scorer identities. It reports failed cells
explicitly without a score. Complete triples yield mean and sample standard
deviation; complete paired triples yield the three treatment-minus-control
deltas. GEC writes a clearly marked derived aggregate receipt before invoking
the isolated official scorer and refuses to score that aggregate again.
Existing output reports are never replaced. Exit 2 can also accompany a saved
report when some cells could not be scored; retain that report and its evidence.

## Execution and resume

The public live command is documented in [research execution](research-execution.md).
It loads candidate-visible packets, segment plans and route configurations from
an explicit runtime map, and loads trusted admission commands and operator
authorizations from separate route maps. The runtime map has no key field.
Relative file references resolve beside each map. Sources routes can be
provided through a private route map or environment assignments.
Request-budget route files and their trusted counter commands come from a
fourth explicit map; they are never discovered from candidate-visible data.

`scheduling.run_research` takes packets, segment plans, the frozen manifest and
execution plan, base route configurations and a private output directory. Its
admission controller must expose `prepare(manifest, plan)` and, for each
segment, receive the nonce-bound request, execution binding, reservation,
remaining ceiling and private evidence directory. It must freshly verify
current pricing, entitlement and capability evidence and return the admission
receipt plus its evidence receipt. The CLI supplies
`CommandAdmissionController`, which invokes only the explicitly supplied
trusted command specs and separately validated operator authorization files. A
callback that merely echoes hashes is suitable for deterministic tests only;
it is not a valid live provider admission probe.

The controller binds its actual prompt/adapter/tool implementation through
`research_implementation_sha256`, including the declared package modules that
implement canonicalization, evidence persistence, packet validation and
reservation arithmetic. This digest is a declared package boundary, not a
freeze of the Python runtime, operating system or transitive dependencies.
Resolved endpoint fingerprints and base configurations must match before
execution. Grading keys are not accepted by the executor. The candidate
receives only its segment, shared instructions, response schema and
condition-authorized reference tools.

A POSIX lock prevents concurrent executors in the same experiment directory.
For v2 paid execution, a separate SQLite transaction serializes commitments
across execution directories, canaries, and scored runs sharing the logical
ledger ID. The private ledger path is outside those directories; its logical ID
and cap must match the portable manifest.
Each reservation is recorded in the append-only attempt metadata before the
provider call. No reservation is reused to fund extra work. A failed segment
stops its cell; later independent cells may proceed. Resume finalizes a started
unfinished attempt as interrupted and never calls it again. No failed cell
receives a partial response aggregate. GEC missing corrections invalidate the
cell; MCQ null remains a deliberate abstention for existing scoring semantics.

Observed model/effort drift, reused or missing session identity, tool/token/cost
overruns, or failed admission stop the whole experiment. A durable stop record
prevents resume from bypassing that decision. Raw results remain preserved.
Reported provider usage is evidence, not a replacement for conservative
pre-request bounds; a post-call overrun check cannot undo an incurred charge.

The whole-packet `pair` diagnostic applies the same missing-or-reused session
check to successful results and validated candidate-response failures. It
validates preserved receipts before new execution, retains the original receipt
when the check fails, and writes a plan-bound `stop.json` so resume cannot skip
the violation. Interrupted and generic failures continue under the existing
resume policy.

If the next immutable segment does not fit, execution stops without retry,
reordering, truncation, or substitution. Immutable `budget-stop.json` records
every remaining scheduled segment as `not_executed_budget` with its denominator.
The durable stop prevents resume, and no complete or subset result manifest is
produced.

Completed cell artifacts bind their ordered attempt receipts, original packet,
segment plan and execution plan. The final result manifest binds all cell
artifacts, candidate and admission receipts, and the complete request-budget
receipt set. These are execution records, not official scores. Offline scoring
must verify the complete sealed cell and scorer/key
bindings before reporting all three repeats, their sample standard deviation,
and complete paired deltas. Never average unrelated suites together.

Fresh controller sessions do not prove independence from provider-internal
caching, training contamination or live corpus overlap. Record those limits in
the experiment report.
