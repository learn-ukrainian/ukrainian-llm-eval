# Results and lessons for Ukrainian training data

## TL;DR

Updated: 6 September 2026. Owner: the evaluator release lead. Tracking: [report maintenance #24](https://github.com/learn-ukrainian/ukrainian-llm-eval/issues/24), [public study #6](https://github.com/learn-ukrainian/ukrainian-llm-eval/issues/6).

- **The primary scored study has not started. There is no ranked leaderboard yet.** Pre-exam synthetic checks establish execution behavior, not Ukrainian proficiency.
- First round: GPT-6 Astra, Claude Fable 5.1 and Gemini 3.8 Flash at low/medium/high; Gemma 4 31B through OpenRouter. Gemma's current catalog has reasoning off/on, not named effort levels; those two modes are the recommended comparison, pending final run-manifest confirmation. No xhigh/max/ultra in this round.
- Compare the same configuration with and without controlled Sources MCP where the route supports both. Native Codex currently supports closed-book only; Gemini's controlled subscription route still needs validation. Unsupported assistance is not a zero score.
- One earlier whole-paper Claude pilot improved from 32/35 to 35/35 with Sources. It is **not** the primary protocol, a result for Fable 5.1, or evidence that reference-assisted training will improve a model.
- The parallel training-data workflow needs to know both where references help and where they introduce mistakes. We will report paired gains/losses, retrieval behavior, unresolved uncertainty, and evidence-linked data priorities. Exam keys and held-out evaluation examples must stay outside training-data generation.

## Leaderboards

Report suites separately; do not combine points, accuracy and correction F0.5 into a single language score. Each completed configuration has three independent repeats. Show every repeat plus its descriptive mean and range; a three-run spread alone is not a population confidence interval. Effort labels are provider-specific and do not represent equal computation across models.

Within each suite, rank complete comparable results separately for closed-book and Sources-assisted conditions. Keep both ranks and their paired difference visible. Rows below are the planned inventory in operator order, **not rankings**. A pending or incomplete cell remains visible and unranked. Link each published score to its immutable run/score evidence; never substitute a synthetic check for a missing score.

| Configuration | Closed-book | With Sources | Current status |
| --- | --- | --- | --- |
| GPT-6 Astra — low | Pending | Unsupported by current native adapter | Admission/pre-exam checks pending |
| GPT-6 Astra — medium | Pending | Unsupported by current native adapter | Admission/pre-exam checks pending |
| GPT-6 Astra — high | Pending | Unsupported by current native adapter | Admission/pre-exam checks pending |
| Claude Fable 5.1 — low | Pending | Pending | Exact native selector/admission checks pending |
| Claude Fable 5.1 — medium | Pending | Pending | Exact native selector/admission checks pending |
| Claude Fable 5.1 — high | Pending | Pending | Exact native selector/admission checks pending |
| Gemini 3.8 Flash — low | Pending | Pending | Controlled subscription route validation pending |
| Gemini 3.8 Flash — medium | Pending | Pending | Controlled subscription route validation pending |
| Gemini 3.8 Flash — high | Pending | Pending | Controlled subscription route validation pending |
| Gemma 4 31B — reasoning off (proposed) | Pending | Pending | Final configuration/admission checks pending |
| Gemma 4 31B — reasoning on (proposed) | Pending | Pending | Final configuration/admission checks pending |

### NMT Ukrainian language

Denominator: the complete 2022 demonstration language block, **20 tasks / 35 points** per repeat. Primary sessions are per complete task. This is not a claim to pass the full multi-subject NMT.

| Configuration | Closed-book rank / points | Sources rank / points | Paired difference (points) | Repeats / task coverage | Evidence |
| --- | --- | --- | --- | --- | --- |
| All planned configurations | Not ranked | Not ranked | Not measured | 0 completed primary repeats | None yet |

### ULP

Denominator: **347 questions** per repeat. Report correct/347 and exact-option accuracy. Paired difference is in percentage points, not relative percent improvement.

| Configuration | Closed-book rank / accuracy | Sources rank / accuracy | Paired difference (pp) | Repeats / question coverage | Evidence |
| --- | --- | --- | --- | --- | --- |
| All planned configurations | Not ranked | Not ranked | Not measured | 0 completed primary repeats | None yet |

### UA-GEC

Denominator: **2,696 sentences in 166 documents**, using both reference annotators. Reassemble the full corpus in source order and score each repeat with the pinned official-compatible scorer; do not average document F0.5 values. Report precision and recall alongside corpus F0.5, with the score scale stated explicitly.

| Configuration | Closed-book rank / P, R, F0.5 | Sources rank / P, R, F0.5 | Paired difference (F0.5) | Repeats / document coverage | Evidence |
| --- | --- | --- | --- | --- | --- |
| All planned configurations | Not ranked | Not ranked | Not measured | 0 completed primary repeats | None yet |

## What Sources changes

For each completed paired comparison, record:

- Same dataset/packet, requested model, effort, configuration, limits and scorer; fresh independent sessions and the approved condition difference. Report observed model/effective effort separately, including unknown values.
- Per-repeat scores and paired item wins, losses and ties for NMT/ULP. For GEC, report corpus precision/recall/F0.5 differences and traceable correction examples only where publication is permitted.
- Whether tools were available, whether the model actually used them, attempted/successful/failed calls, retrieved evidence identifiers, time, token usage and observed cost. Tool availability does not prove tool use; a successful call does not prove the answer used its evidence correctly.
- Helpful retrieval, irrelevant or conflicting retrieval, unsupported assertions, harmful overcorrection and valid unanswered ambiguity. Label automatic error categorization as provisional; no human linguistic review is currently available. Unsupported causal explanations stay hypotheses.
- Service/tool-schema identities and observation times where available. Sources is live and being expanded; retain observed drift and possible overlap with public exams. Do not claim a frozen snapshot, training-unseen questions, or attribution solely to MCP when conditions changed in other ways.

Retain wrong answers, malformed responses, tool omissions, timeouts, budget stops and interrupted attempts under the frozen protocol. Include candidate failures in the intended score denominator; show execution coverage separately. Do not hide incomplete runs, repair answers, select the best repeat, or drop a model because its results are poor. Implementation/admission failures remain distinct from language mistakes.

## Lessons for the training-data workflow

This section is a maintained evidence register, not a list of assumed model weaknesses. No primary-study finding has been established yet.

| Finding | Evidence and confidence | Possible training-data action | Validation before adopting it |
| --- | --- | --- | --- |
| No primary-study findings yet | No completed primary exam cells | No dataset change justified by this report yet | Complete paired evaluation and inspect failure evidence |

Add rows with a stable finding ID, affected suite/model/effort, counts and denominator, paired direction, run/score references, observed retrieval behavior, confidence/limitations, and a linked dataset-project issue plus owner/status. Distinguish missing knowledge, failure to retrieve, failure to apply retrieved evidence, format/tool failures, and scorer/reference ambiguity; do not force every failure into a grammar category.

Possible interpretations to test, not current findings:

- Better assisted answers can motivate source-grounded examples that teach retrieval and application. They do not establish that a fine-tuned model will retain the same knowledge without tools.
- Correct references followed by wrong answers can motivate examples of applying a rule, but first rule out ambiguous questions, retrieval mismatch and scoring errors.
- Worse assisted correction can motivate examples that preserve acceptable Ukrainian forms and handle conflicting evidence; do not automatically treat every reference disagreement as a model defect.
- Similar performance with no tool calls says little about the quality of the Sources corpus. Separate the model's tool policy from corpus coverage.

Keep evaluation and training partitions separate, with provenance and overlap checks across benchmark text, answer keys, near-duplicates and retrieved material. Sources-backed data generation must not silently ingest the held-out exam material exposed during error analysis. Develop teaching examples from separately licensed material and validate dataset changes on a distinct held-out evaluation. If this public exam is used to guide training, disclose that use and stop describing later performance on it as unseen generalization. Retrieval-assisted exam gains are not measured training gains; the latter require a separate before/after training evaluation.

## Historical pilot — excluded from the primary leaderboards

The [original reviewed prototype PR](https://github.com/learn-ukrainian/learn-ukrainian.github.io/pull/7699) records one whole-paper paired run on Claude Sonnet 5, requested high effort, at source head `31638b7c2a874760be470a6048bbc998882b2bb2`.

| Condition | Raw points | Fully correct tasks | Reference calls |
| --- | --- | --- | --- |
| Closed-book | 32/35 | 17/20 | 0 |
| Sources-assisted | 35/35 | 20/20 | 13 |

Descriptive difference: +3 points; three task wins, no losses, 17 ties. This was one pair with whole-paper sessions and the then-current private corpus. It is not the three-repeat segmented study, is not evidence for the revised shortlist, and cannot establish a general MCP benefit. Earlier engineering failures remain preserved outside this pilot summary. Do not pool this pilot or synthetic checks into the primary leaderboard.

## Maintenance and evidence links

Update this document after each completed scored cell, paired comparison or material execution failure, and at release. Every update must:

1. Refresh the TL;DR, dated progress/coverage, affected suite table and limitations together. Pending runs remain pending; label interim results explicitly.
2. Bind published rows to experiment/configuration IDs, requested and observed identity/effort, repetition IDs, source/packet/scorer hashes, immutable result/score receipts and Sources identity observations. Retain denominator and failure/status counts.
3. Add a dataset-learning entry only when traceable evidence supports it; otherwise record the question for investigation. Link follow-up issues without copying private content.
4. Keep concise summaries and privacy-safe evidence manifests in Git. Put large permitted evidence in versioned archives with checksums and durable links; never commit keys, credentials or private raw retrievals. Unpublished evidence means readers cannot independently verify that portion; say so.
5. At each release, link an immutable report revision and downloadable evidence manifest from the release notes. Keep this document as the evolving view without rewriting historical artifacts or moving tags.

No public study evidence archive exists yet. The current historical pilot is supported by its linked PR summary, not a publicly downloadable raw trace. See the [benchmark protocol](benchmarks.md), [research execution](research-scheduling.md) and [release procedure](releasing.md).
