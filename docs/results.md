# Results and lessons for Ukrainian training data

## TL;DR

Updated: 6 September 2026. Owner: the evaluator release lead. Tracking: [report maintenance #24](https://github.com/learn-ukrainian/ukrainian-llm-eval/issues/24), [public study #6](https://github.com/learn-ukrainian/ukrainian-llm-eval/issues/6).

- **The primary scored study has not started. There is no ranked leaderboard yet.** Pre-exam synthetic checks establish execution behavior, not Ukrainian proficiency.
- First round: GPT-6 Astra, Claude Fable 5.1 and Gemini 3.8 Flash at low/medium/high; Gemma 4 31B through OpenRouter. Gemma's current catalog has reasoning off/on, not named effort levels; those two modes are the recommended comparison, pending final run-manifest confirmation. No xhigh/max/ultra in this round.
- Every selected model must pass synthetic readiness checks in both closed-book and controlled Sources modes before any scored exam starts. The restricted native Codex policy in [draft PR #27](https://github.com/learn-ukrainian/ukrainian-llm-eval/pull/27) now passes local isolation controls and six live synthetic canaries; independent review and formal study admission remain pending. Gemini's controlled subscription route still needs validation. Admission failures are not zero scores.
- Fable 5.1 has passed six live synthetic canaries: low/medium/high in both conditions, with matching observed model identity and one reference call in each Sources case. Effective effort remains unreported, and these canaries do not complete benchmark admission.
- Gemma passed both closed-book canaries but failed both Sources canaries: with reasoning off and on, it skipped the required lookup and returned the wrong option. All four charges settled against the existing cap, totaling $0.000614 after conservative rounding. These failures remain in the evidence; no scored exams started.
- One earlier whole-paper Claude pilot improved from 32/35 to 35/35 with Sources. It is **not** the primary protocol, a result for Fable 5.1, or evidence that reference-assisted training will improve a model.
- The parallel training-data workflow needs to know both where references help and where they introduce mistakes. We will report paired gains/losses, retrieval behavior, unresolved uncertainty, and evidence-linked data priorities. Exam keys and held-out evaluation examples must stay outside training-data generation.

## Leaderboards

Report suites separately; do not combine points, accuracy and correction F0.5 into a single language score. Each completed configuration has three independent repeats. Show every repeat plus its descriptive mean and range; a three-run spread alone is not a population confidence interval. Effort labels are provider-specific and do not represent equal computation across models.

Within each suite, rank complete comparable results separately for closed-book and Sources-assisted conditions. Keep both ranks and their paired difference visible. Rows below are the planned inventory in operator order, **not rankings**. A pending or incomplete cell remains visible and unranked. Link each published score to its immutable run/score evidence; never substitute a synthetic check for a missing score.

| Configuration | Closed-book | With Sources | Current status |
| --- | --- | --- | --- |
| GPT-6 Astra — low | Live synthetic passed; identity unknown | Live synthetic passed; identity unknown | Draft implementation; independent review pending |
| GPT-6 Astra — medium | Live synthetic passed; identity unknown | Live synthetic passed; identity unknown | Draft implementation; independent review pending |
| GPT-6 Astra — high | Live synthetic passed; identity unknown | Live synthetic passed; identity unknown | Draft implementation; independent review pending |
| Claude Fable 5.1 — low | Live synthetic canary passed | Live synthetic canary passed | Model identity matched; effective effort unknown; admission pending |
| Claude Fable 5.1 — medium | Live synthetic canary passed | Live synthetic canary passed | Model identity matched; effective effort unknown; admission pending |
| Claude Fable 5.1 — high | Live synthetic canary passed | Live synthetic canary passed | Model identity matched; effective effort unknown; admission pending |
| Gemini 3.8 Flash — low | Pending | Pending | Native selector listed; isolation and live admission unverified |
| Gemini 3.8 Flash — medium | Pending | Pending | Native selector listed; isolation and live admission unverified |
| Gemini 3.8 Flash — high | Pending | Pending | Native selector listed; isolation and live admission unverified |
| Gemma 4 31B — reasoning off | Live synthetic passed | Failed: omitted lookup; wrong answer | Model/provider matched; admission blocked |
| Gemma 4 31B — reasoning on | Live synthetic passed | Failed: omitted lookup; wrong answer | Model/provider matched; admission blocked |

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

## Native Codex execution controls

The subsequent restricted-catalog implementation at source head `9b37fd1` in [draft PR #27](https://github.com/learn-ukrainian/ukrainian-llm-eval/pull/27) passes **27/27 local synthetic control cases**: nine cases at each requested low/medium/high effort, using all 13 permitted live Sources tool schemas. These controls were repeated from a clean wheel installation. Successful cases also check that an explicit final-output file separates the final answer from progress messages. No provider inference occurs in that local matrix.

The nine cases cover an empty closed-book surface, a permitted call, three denied resource operations, call-cap overflow, an unlisted tool, schema drift and a missing tool. Denied operations never reach the synthetic upstream; missing/changed schemas stop before the fixture receives a model request. The full test suite passed 490 tests, with lint also passing. These are implementation checks, not Ukrainian language scores.

Final installed control-report SHA-256 identifiers: low `527d8354fffea81dd686221088e528dcab148debee162b3126170b2d8be8e4c3`; medium `1f85de04a28b009d25145380d26e8b7ff164862a5d21db581e0fc34b6665f6e7`; high `47fc9e637fc3a984a744b6fb4d442a2b68fb4a343339056d7da4e1c273809c2e`. Raw captures remain private, so these reports are not independently downloadable yet.

Six fresh live synthetic canaries passed through the native ChatGPT subscription route after the final-output fix: low/medium/high in both conditions. Closed-book returned the expected exact-token answer with zero tool calls; Sources returned the expected tool-result count with one reference call each. All six evidence receipts and archive checksums verified. Effective backend model and effort remain `unknown` in the native protocol; requested labels are not attestation. Summary SHA-256: `39ad93f7636c9ea4c51db2929c6ab20b5dd3d0f88584e6d1aa3b36f45aba6b25`.

The first live low-effort Sources attempt remains recorded as failed. Its lookup succeeded and the CLI emitted correct final JSON, but the adapter concatenated a progress message with that answer. The fix uses the CLI final-output file, checks it against the final streamed message, and retains malformed final answers as failures. Fresh local controls preceded the six new live attempts; the original failure was not relabeled. These canaries establish execution behavior, not exam scores or complete study admission.

The earlier baseline failure below is retained as historical engineering evidence; the new policy explicitly restricts catalog tool settings instead of relying on feature-disable flags alone.

A local-only capture on 6 September 2026 used fresh empty homes, neutral working directories, a loopback Responses fixture and a synthetic MCP server. It supplied no credentials, made no provider inference call and used no scored exam material. Scope: GPT-6 Astra requested at low effort; medium/high readiness remains untested by this capture.

| Check | Observed outcome | Admission consequence |
| --- | --- | --- |
| Existing closed-book control suite | Failed: unexpected collaboration and asynchronous-input tool descriptions; delegation injection did not establish an inert handler | No fresh closed-book control receipt issued |
| Direct MCP call with Code Mode host disabled | MCP initialized and listed tools; direct reference call rejected | Connection startup alone does not establish usability |
| Code Mode host enabled, synthetic reference call | Returned the synthetic reference marker | Connection works, but reference-only isolation remains unproven |
| Code Mode nested tool inventory | Included `apply_patch`, a clock tool and MCP resource helpers alongside the allowed synthetic reference tool | Broader than the permitted reference allowlist; no Sources readiness claim |

The capture identifies CLI `0.153.4`, native-runtime SHA-256 `b973d440acac501fd2594a43e7ca9ce41e0a65b9dfb28d0d7a7837c99e1261e3`. The successful synthetic-call capture SHA-256 is `b05725c7357a767f1c0e96638448c20e0a22def09d3695c9afad8912329e43ac`. These are local engineering observations; raw captures are retained privately and are not yet independently verifiable from a public evidence archive. They establish neither live subscription readiness nor language proficiency.

Owner: evaluator release lead. Next action: obtain independent exact-head review of the draft implementation, then complete live admission/readiness for every selected configuration in both conditions. Review delegation remains paused under the operator's no-subagents instruction. Track implementation in [#26](https://github.com/learn-ukrainian/ukrainian-llm-eval/issues/26). No scored runs may proceed while the all-model readiness gate is incomplete.

## Native Claude execution controls

Six live synthetic canaries passed on 6 September 2026 through the first-party native subscription route: low/medium/high in both conditions. Every attempt reported effective model `claude-fable-5-1`; effective effort remained `unknown`. Closed-book cases returned the expected exact-token answer with zero reference calls. Sources cases returned the expected tool-result count with one reference call each. All six evidence receipts verified, and the private archive checksums matched. Summary SHA-256: `be13529784de3f4cd17824a855aabcdc2d15f06a5301fce56a249e04a6073d80`.

The driver stopped once because it tried to overwrite an immutable progress-summary file after saving a completed attempt. Recovery verified the saved results and resumed only unattempted cells; no completed model call was repeated. These are synthetic checks, not exam scores, and neither subscription expiry nor complete benchmark admission is asserted.

Local captures on 6 September 2026 requested `claude-fable-5-1` at low/medium/high through the existing adapter, redirecting inference to a synthetic server with a dummy API key and separate configuration directory. All six effort/condition combinations sent the requested model string and matching effort field. Closed-book requests exposed only `StructuredOutput`; Sources requests added exactly the 13 reference tools. This proves request construction, not subscription eligibility or the provider's accepted model/effort.

At every effort, an injected reference call returned the synthetic marker. A second injected call with a configured cap of one returned `Reference call limit reached`; only the first reached the synthetic upstream. Captures ended intentionally with a synthetic HTTP error, so these are not successful answer canaries. The server observed two requests in each request-only capture and four in each call-cap capture; native retries remain visible. No provider inference or scored exam material was used. Broader adversarial isolation and live admission remain pending.

The verified summary SHA-256 is `f42989148892a94fe69fdd20d0f67dfd652c777cee9ea6924915386eedb309ff`. Captures, scripts and binary/adapter identities are archived privately; no public raw archive is available. Owner: evaluator release lead, tracking [provider readiness #5](https://github.com/learn-ukrainian/ukrainian-llm-eval/issues/5).

Follow-up captures passed six file/shell denial and ambient-instruction checks: every effort in both conditions rejected injected `Read` and `Bash` calls as unavailable, and neither the planted file contents nor the planted `CLAUDE.md` instruction entered subsequent requests. Six further captures completed valid synthetic structured answers through the adapter; Sources cases included one successful synthetic reference call. An earlier fixture used an incorrect item ID and was rejected by the answer schema; those failed captures remain preserved. All answers were scripted locally, not generated by Fable. These checks still do not establish live identity, effective effort, subscription admission or complete adversarial coverage.

Follow-up summary hashes: denial/ambient `24ac65bf9e567262903d0d21b091dc33e9b4ad8555a4821dba8440d0d175aa6b`; successful synthetic answers `1efb99277c82effd203b92e9a924d7c4a77f9b995b50578b6e40dbc55e238cc8`. Their raw evidence also remains private.

## Gemini route investigation

On 6 September 2026, the installed AGY CLI catalog listed `gemini-3.8-flash-low`, `gemini-3.8-flash-medium` and `gemini-3.8-flash-high`. This establishes selectable names only. No Gemini model request or isolation control ran during this investigation, and none of these six model/condition combinations is admitted yet.

The documented [headless behavior](https://www.antigravity.google/docs/cli/headless/) permits a successful exit after a tool is denied and allows workspace file access by default. Its stream includes tool inventory and tool events, which must be inspected alongside the answer. The [permission rules](https://www.antigravity.google/docs/cli/permissions/) describe explicit denials and per-tool MCP permissions, but the installed CLI help does not expose a general session tool allowlist or settings-file override. Configuration isolation and enforcement still need installed behavior proof; documentation alone does not establish either. The [SDK quickstart](https://www.antigravity.google/docs/sdk/overview/) uses a Gemini API key or Vertex credentials, so it has not been accepted as a substitute for the selected subscription route.

Owner: evaluator release lead, tracking [provider readiness #5](https://github.com/learn-ukrainian/ukrainian-llm-eval/issues/5). Next action: establish an isolated native configuration, then test ambient file, web, command and delegation denial plus the controlled Sources allowlist and call cap before live admission. If the native interface cannot demonstrate those controls, retain the route as unavailable rather than silently substituting paid access.

Two no-prompt AGY startup probes selected workspace profiles using the documented directory and flat-file layouts. One declared an empty tool list; the other allowed only `finish`. Both startup inventories still listed 57 tools, including file, shell, web and delegation tools. The profile-list command returned no entries, so correct profile discovery/application is also unproven. These startup observations fail the evaluator's restricted-surface check; no Gemini inference was attempted. The separate Gemini CLI is being investigated as a native subscription alternative, with no paid API substitution.

A locally installed Gemini CLI probe used a dummy API key and loopback fixture. Its initial request targeted Gemini 3.5 Flash/high despite the Gemini 3.8 Flash startup label; the documented dynamic-model configuration corrected the request to Gemini 3.8 Flash/low with the configured output cap and no function declarations. A planted `GEMINI.md` instruction still entered the request because the configured context filename supplements the defaults. That unguarded configuration failed isolation; no live Gemini inference or admission followed. Failed and corrected captures are retained privately.

A follow-up launch guard rejected all nine planted-file cases across low, medium and high effort before CLI startup. Fresh empty-workspace captures passed at all three efforts: exact model and effort, capped output, and no callable tools. These are local fixture checks, not subscription eligibility proof. An empty `tools.core` list also creates a policy denying MCP tools. Adding the single qualified fixture name (`mcp_sources_verify_word`) allowed a successful reference call and returned its result to the next request at all three efforts; the fixture's forbidden tool and built-in tools remained absent. The initial assertion incorrectly expected an unqualified name; a separate corrected assessment preserves the original captures and failed assertion. A subsequent check used the evaluator's full 13-tool proxy at all three efforts. Each run advertised exactly those reference tools, forwarded two calls under a two-call cap, rejected the third call, and rejected an upstream tool outside the allowlist. These are local mechanism checks; wider adversarial denial coverage and live native admission remain outstanding.

No-prompt native ACP probes initialized successfully but failed session authentication with error `-32000`, before a model catalog was returned. Existing native credentials were projected into an isolated home; no API-key fallback or model prompt was used. The failure does not establish whether credentials, entitlement or connectivity caused it. CodexBar also returned an error, leaving Gemini quota unknown. Live Gemini readiness remains unproven.

## Gemma execution controls

Four live, unscored synthetic canaries used `google/gemma-4-31b-it` through OpenRouter, requesting the Venice BF16 endpoint with fallback disabled, a 4,096-token output cap and the existing shared $10 spending ledger. Responses identified the expected model and Venice provider; the endpoint precision and effective reasoning switch were not independently attested. Observed reasoning-token counts were zero in both off requests and 150/87 in the on closed-book/Sources requests. No named effort selector was supplied.

Both closed-book cases returned the expected exact-token option with zero tool calls. Both Sources cases advertised all 13 permitted tools but made **zero calls** and returned B instead of the expected A. A separate live VESUM check confirmed the expected six matches before these calls. Transport status was `ok`; semantic readiness was **2/4**, with **0/2 Sources passes**. No retry or answer repair was performed. Tool availability and successful HTTP completion do not establish usable retrieval.

The installed adapter passed 10 local fixture controls covering reasoning off/on, closed-book and Sources requests, forbidden tools, call-cap overflow and provider mismatch. All eight live evidence stores (four candidate, four budget) verified. Each charge settled from the provider's account-charge field, conservatively rounded upward to micro-USD: 31 and 95 for closed-book, 226 and 262 for Sources; total **614 micro-USD ($0.000614)**. This is new-spend-cap accounting, not a claim about the account's unknown prepaid balance.

The canaries used source head `017ccaf` from [draft budget fix PR #29](https://github.com/learn-ukrainian/ukrainian-llm-eval/pull/29), which permits authoritative account-charge routes to share the existing policy with conservative usage-bound routes. Its full suite passed 484 tests and all CI checks. Independent review remains pending under the operator's no-subagents instruction. Private live-summary SHA-256: `9bbd413028ea70beb850fa1e9f404373bb118618585eae0851ee415b4306a21b`; raw evidence is not publicly downloadable.

Two separately frozen diagnostics changed only `http_response_format` from `json_schema` to the existing `json_object` option, one per reasoning mode. Both again advertised 13 tools, made zero calls and returned B instead of A. All four diagnostic evidence stores verified; charges settled at 226/275 micro-USD, adding $0.000501 (Gemma total including diagnostics: $0.001115). No original result was replaced. Diagnostic-summary SHA-256: `2f9598a789bba866b235db41d4ab60dff56878b82a008aab9171e713768ea116`.

Removing the strict schema alone did not restore tool use in those two observations. Both formats still constrain output to JSON, so this does not isolate unconstrained generation or prove a model defect. [Venice's structured-response documentation](https://docs.venice.ai/guides/features/structured-responses) states an incompatibility with parallel function calls; that restriction alone does not explain the observed zero-call responses. Its [function-calling examples](https://docs.venice.ai/guides/features/function-calling) omit `response_format`. The installed evaluator currently exposes only `json_schema` and `json_object` for this adapter.

Owner: evaluator release lead. Next action: prepare a bounded unconstrained-response diagnostic with explicit configuration and budget/evidence binding before any further live attempt. Preserve all failed observations and keep the all-model, both-mode exam gate closed.

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
