# Segmented research execution

Primary research uses one fresh session per complete NMT task, ULP question,
or UA-GEC document. Matching rows stay together. Each model/effort/condition
cell must cover the whole frozen suite before receiving a score. Whole-packet
`run` and `pair` are separate endurance diagnostics.

The Python interfaces below implement the controller foundation. The research
CLI, offline cell-scoring/aggregation commands and provider-specific admission
probes remain work in issue #3/#5. Do not substitute the diagnostic `pair`
command for a primary research experiment.

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
scorer identity and pricing/entitlement evidence. Limits explicitly apply to
each segment. The generic constructor permits deliberate repeat counts; the
public experiment requires three. Unsupported conditions need an evidence hash
explaining their absence and remain visible in the experiment definition.

`build_execution_plan` constructs every ordered cell and reservation. Conditions
alternate order across repeats. It rejects a total reservation above the shared
new-spend ceiling, initially USD 10. Amounts use integer micro-USD; metered rates
are per million tokens and multiplication rounds up. Input bounds must cover
all requests, including repeated histories and tool results. Output bounds
cover all permitted tool rounds. Unknown pricing is not zero cost. Subscription
or existing-credit zero incremental cost requires separate entitlement evidence.

These pure constructors bind supplied evidence; they do not authenticate a
provider, prove entitlement, or authorize spending.

## Execution and resume

`scheduling.run_research` takes packets, segment plans, the frozen manifest and
execution plan, base route configurations and a private output directory. Its
required `admission_probe(route, config, condition)` controller callback must
freshly verify current pricing, entitlement and capability evidence and return
their manifest hashes. A callback that merely echoes those hashes is suitable
only for deterministic tests. It is not a valid live provider admission probe.

The controller binds its actual prompt/adapter/tool implementation through
`research_implementation_sha256`. Resolved endpoint fingerprints and base
configurations must match before execution. Grading keys are not accepted by
the executor. The candidate receives only its segment, shared instructions,
response schema and condition-authorized reference tools.

A POSIX lock prevents concurrent executors in the same experiment directory.
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

Completed cell artifacts bind their ordered attempt receipts, original packet,
segment plan and execution plan. The final result manifest binds all cell
artifacts and the ordered receipt set. These are execution records, not official
scores. Offline scoring must verify the complete sealed cell and scorer/key
bindings before reporting all three repeats, their sample standard deviation,
and complete paired deltas. Never average unrelated suites together.

Fresh controller sessions do not prove independence from provider-internal
caching, training contamination or live corpus overlap. Record those limits in
the experiment report.
