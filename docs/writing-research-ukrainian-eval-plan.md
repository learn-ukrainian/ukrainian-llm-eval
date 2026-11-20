# Writing, research and Ukrainian-language evaluation: plan

Status: **plan and canaries only; no full run has started.** Tracking: the operator evaluation order of 2026-11-19 (issue 13212 in the fleet tracker) and a scope addition from `claude-cto` (message sent 2026-11-20T21:25Z, read by the evaluator at 21:54Z and applied here). Owner: the evaluator lead; accountable lead for closure: `claude-cto`. Date: 2026-11-20.

## 1. Outcome and the decision it informs

The fleet wants to know where Gemini 4 Neon is strong or weak against the models it would replace or sit beside, on three kinds of work: **writing** (project documentation and KubeDojo teaching content), **research** (briefs from a source packet or from the Sources tools) and **Ukrainian-language capability**. The Ukrainian part carries the scope addition: every model runs the same items **twice, once with no tools and once with the Sources MCP tools**, and the headline is the **difference the tools make, per model and per failure class**: Russian-inferred forms and Russianisms, stress, inflection, aspect pairs, case government, surzhyk and calques, cultural and historical facts. The deliverable is a table of per-model strengths and weaknesses with the evidence behind each cell. It changes no routing and certifies nothing.

**Simplest design that meets the outcome** (chosen; nothing larger was needed): the same frozen prompt, one fresh read-only session per model per task and condition, through the existing dispatcher; objective keys wherever a right answer exists; blind cross-family model judges with hidden checklists elsewhere; a deliberately defective control in every judge bundle. No new service and no new code in the product package; the harness is one standard-library script whose hash is frozen below.

**Reused design.** The scope message asked to reuse the "war-plan D Sources-MCP A/B eval" from this repository. No document of that name exists in the repository's `docs/` or Git history (searched); the repository's own study design is the paired closed-book/Sources condition on an identical item sample, with per-item paired differences. This plan reuses that: identical items across both conditions, the answer first and a short unscored explanation after, and a bug-fix rule (below): a harness defect found by a control is fixed and **all** models are rerun on the affected cells, never a subset.

## 2. Arms

| Arm | Family | Route and effort | Notes |
|---|---|---|---|
| Gemini 4 Neon | Google | native AGY, `gemini-4-neon-high`, effort high | the model under study |
| Claude Opus 5.5 | Anthropic | native Claude, `claude-opus-5-5`, effort high | |
| GPT-6.2 Sol | OpenAI | native Codex, `gpt-6.2-sol`, effort high | |
| GPT-6.1 Terra | OpenAI | native Codex, `gpt-6.1-terra`, effort high | candidate only; its catalog entry gives it no Ukrainian-content judgment or review role |

**Savant is paused by operator order (2026-11-19) and is excluded from every role.** The originating issue named Savant for the Ukrainian suite; Opus 5.5 takes its place so all four arms face the same Ukrainian items, and the deviation is recorded. Requested effort is recorded; effective effort is **not attested** for any arm.

Arms do not see identical surroundings, and this is a limitation, not a control: each route loads its own harness context (observed per-call input of about 150k–275k tokens including cache), and only the Claude route exposes web tools. Every candidate prompt forbids web and file access and states the Ukrainian source rules inline. Compliance is audited from the dispatcher's records (section 10) and reported per arm.

## 3. Held-out task set: 26 prompt variants per arm, 104 generations

"Held-out" means: written for this study, never published, kept out of the repository until after the run, not copied from any KubeDojo module (the three KubeDojo topics have no dedicated module). SHA-256 hashes of the exact prompt bytes are frozen below; full prompts, keys, outputs and traces stay in evaluator-held artifacts outside Git. Contamination of the task topics by public web text cannot be excluded.

| Suite | Tasks | Conditions | Per arm | What each asks | Scored by |
|---|---|---|---|---|---|
| Docs (WD) | 3 | no tool | 3 | runbook, decision record, README usage from a fact packet with a planted contradiction, a missing fact and a distractor | hidden checklist, judges, word limits |
| KubeDojo (WK) | 3 | no tool | 3 | module fragment (outcomes, scenario, worked YAML and commands, quiz) on PodDisruptionBudgets, topology spread, ResourceQuota/LimitRange | checklist of stable Kubernetes facts, judges, structure |
| Research (RB) | 4 | RB1, RB2 no tool; RB3, RB4 Sources | 4 | vendor-vs-independent packet; SLO data with a missing and a mismatched metric; Ukrainian lexical and orthography briefs grounded in Sources tools that must mark unsupported claims | checklist, judges (RB3, RB4 by Ukrainian seats) |
| Ukrainian writing (UW) | 2 | **both** | 4 | A2 pharmacy dialogue; B2 formal email | judges within a condition, `check_text` evidence, word limits |
| Ukrainian keyed (UK) | 6 sets, 81 items | **both** | 12 | UK1 inflection 16; UK2 Russianisms 10 defects + 4 controls; UK3 stress 10 + spelling 8; UK4 aspect 6 + case government 5; UK5 Russian-inferred forms 2, surzhyk 4, calques 3 + 4 controls; UK6 cultural and historical facts 9 | answer key, no judge |

Per arm: 10 single-condition tasks + 4 UW prompts + 12 UK prompts = 26 generations; **104 generations**. Ukrainian keyed answers come first as a JSON `answers` object; a short `explanations` object follows and is **never scored** (kept for error analysis).

**Failure classes and item counts** (the headline unit): inflection 16; Russianisms 10 plus Russian-inferred forms 2; stress 10; orthography 8 (outside the requested list, reported for completeness); aspect pairs 6; case government 5; surzhyk 4; calques 3; cultural and historical 9; clean controls 8 (false-alarm rate, reported apart). Several classes are tiny: a paired closed-book vs Sources difference smaller than three items in a class of ten is not distinguishable from noise, and classes of 2–6 items are descriptive only.

**Ukrainian items were verified against the Sources tools before freezing.** UK1–UK3 (48 items): 123 calls (47 `search_style_guide`, 31 `search_text`, 22 `check_russian_shadow`, 11 `query_pravopys`, 9 `verify_words`, 2 `inspect_words`, 1 `verify_stresses`). UK4 and UK6: 44 calls (`query_sum20`, `search_style_guide`, `search_text`, `query_wikipedia_cached`, `search_external`, `verify_words`, `query_ulif`). UK5: 79 calls (28 `search_style_guide`, 17 `search_text`, 15 `check_russian_shadow`, 15 `search_heritage`, 3 `verify_words`, 1 `inspect_words`). Rules applied: VESUM for every key form and its tags; the ULIF stress oracle (single reading only) for stress keys; Правопис 2019 with § locators for spelling keys; both `search_style_guide` and source-scoped `search_text` on Антоненко-Давидович plus the shadow check for every Russianism, surzhyk or calque defect and every clean control. A previous study's two mislabelled controls came from skipping that query, so every control here was queried. Evaluator review then removed seven items that were contested or weakly sourced (their ids and reasons are in the private evidence), and one key accepts a VESUM-attested variant. Defect items are scored for detection and for correction separately, so a disputed correction key cannot decide a detection score. Known weaknesses: UK5's four controls are the literal corrections of four defects in the same set, so a model can solve them by contrast; UK6 answers each rest on one source (textbooks and lesson notes); UK4 P12 rests on usage, not an explicit rule; two stress keys (S1, S6) rest on one ULIF reading. For the two Ukrainian research briefs, the checklist rewards marking an unsupported claim UNVERIFIED over inventing one.

Prompt hashes (SHA-256 of the byte-identical prompt every arm receives):

| Task | Condition | SHA-256 |
|---|---|---|
| WD1 | single | `7314271acdec53a76b7b9ee1d99b143faef24fd4a8d0c530d3c52e656c7f4be3` |
| WD2 | single | `dee170d7df0dbf0d038b15c4a77309814fbbfc4f6057802bea907509507eb8d2` |
| WD3 | single | `44028d868d78176f1ee64dc46470fc6aa5fe25a32958f4543f9157ac80d7a33a` |
| WK1 | single | `b39e963b8c25cfd2ec26146e3b2f5666a2b5da86ea6fe31d9770ff36ba3674b5` |
| WK2 | single | `d7b8affe0a955ec450ddcc7f83f2f33b8991d9382c8ba5a0b9218d9607662bb2` |
| WK3 | single | `2be6cc8bf6100662f711d747d738628accd7eff6c2e5884ad518c7be391fa47b` |
| RB1 | single | `8af969af365c63166af060af41945337c38bdc0b11ae014459d2524c45fab9c2` |
| RB2 | single | `70a15bdcfe6cad8f5c9f43c34e289d8efb19859fcd9dcf5166d334cf1788e6eb` |
| RB3 | single | `06cfdbdfd6dc9290dd676156654a0097707f73d7ab4d49d5e95cfef82567aecc` |
| RB4 | single | `6461299b7a2728bc2fadffcfcd646e011fd20c653ca74b08c621c75e6302d067` |
| UW1 | closed-book | `f2fe9c86b8aa14dab934f8f4e21c019186fb5767f1737c65574e6d7ff53a00e8` |
| UW1 | Sources | `603fb5b820742c3d78b59dee286e74e6be6cf926a11137cfbc8554fd35da7b41` |
| UW2 | closed-book | `4f9d8a96033e2cf892e89ff685889d168717466e94292aeefbb9fbbc801484aa` |
| UW2 | Sources | `5e0532a2376ff6b105ba492291c9f6b391417e4d9849b7d1cabbe8d04fb861ef` |
| UK1 | closed-book | `0d5b206b612b6e88d075f2988b323ff6eb37bbedd82c8efd37e38fee6a465218` |
| UK1 | Sources | `b29e1d0a7fc95a22cf31863ee941e74d44a68beaf6a525a8d818eb356743d6b7` |
| UK2 | closed-book | `196f00568114b2339847529cee05909d55585dd006ec44bdc7f121d47a8cc1b2` |
| UK2 | Sources | `f0b755ea927be36fbf7cd2cd796de5d0288d3326bee153d696d1ef8e7b464038` |
| UK3 | closed-book | `b5e7b9a55f575b903735c7061ecf9b052b8be2692165e6e8772109f2154e58ec` |
| UK3 | Sources | `244321e662f254a8395088fcfc04b65798305b1d5f313db466b38fae97ed5020` |
| UK4 | closed-book | `a49a065a00c4990110a757dccf9c4d104dfb9cdca9f4d970159cee5e0b0faaa2` |
| UK4 | Sources | `38404a5ed5e5ce0cff4d30833815fe60d7f55993a807759b9a75aaa74176f439` |
| UK5 | closed-book | `97b30d880714cf08defc4dcca08ed07e80518be33d9bf0676d49804cfe48a115` |
| UK5 | Sources | `996b6221aef8a8c07fdcddae4880b6208829bc8dc9140876c04fdc8ff3ba4b33` |
| UK6 | closed-book | `983292132760e92b90b5b44e9f27be6ab9a0ee682b45a11bfcf642144c680a81` |
| UK6 | Sources | `1b3f1566f87ea11017d278f7fd53bd0f5d1e31c0545d2f9094357dcd2071884b` |

## 4. Rubric and scoring

- **Keyed (UK):** exact-key match after normalisation (apostrophe variants, case, accent position for stress; word-boundary match for year/name answers in UK6). Defect items score detection (any non-OK answer) and correction (an accepted replacement); controls score only OK. The scorer was run on perfect, empty and wrong answers for all six sets (81/81 perfect, 0 empty after a string-key bug was found and fixed).
- **Mechanical constraints:** word counts and required structure are computed by script.
- **Ukrainian free text (UW, RB3, RB4):** `check_text` (VESUM, Russian shadow, UA-GEC) on every output; findings are evidence, shadow and UA-GEC hits are suspicions, not verdicts.
- **Judged tasks:** each judge scores every visible candidate 0–4 on c1 fidelity, c2 gaps and conflicts, c3 fitness and constraints, c4 language, ticks the hidden checklist, lists concrete defects, ranks all candidates with no ties, and guesses each candidate's model family last as a blindness audit.
- **Tool effect:** for every UK item and class, closed-book correct vs Sources correct per model, with Sources call counts from the dispatcher record; cases where the tools made an answer worse are listed.

## 5. Blind cross-family judging

A judge never scores its own family. Judges: Claude Opus 5.5, Gemini 4 Neon, GPT-6.2 Sol. **Terra never judges** (no Ukrainian-content or review role). **Opus never judges Ukrainian work** (Ukrainian judging seats are GPT or Gemini). UW outputs are judged **within** a condition: the closed-book bundle and the Sources bundle are separate calls.

| Judged output | General tasks (WD, WK, RB1, RB2) | Ukrainian tasks (RB3, RB4, UW1, UW2, each condition) |
|---|---|---|
| Opus | Neon, Sol | Neon, Sol |
| Neon | Opus, Sol | Sol |
| Sol | Opus, Neon | Neon |
| Terra | Opus, Neon | Neon |

Blinding: labels X1… are shuffled per judge call with a seed from the task and judge; judge prompts are byte-identical apart from the shuffled candidates; each bundle carries one deterministic **degraded control** (the first 60% of a candidate's blocks, lines or sentences, never cutting a fenced block, plus one fabricated sentence). The cloned arm rotates by a hash of task and judge, and the choice is recorded. Judge calls: 8 general tasks × 3 judges + 6 Ukrainian bundles (RB3, RB4, UW1 and UW2 in two conditions) × 2 judges = **36**. Total dispatches: **104 generations + 36 judge calls = 140**.

The evaluator is Anthropic-family: Claude Sonnet 5.5 and the Anthropic-family sub-agent sessions it delegated item verification to authored the tasks, keys and checklists. It performs only mechanical work in the run (blinding, unblinding, aggregation, hashes) and judges nothing. One author family wrote the set, so an Anthropic-style preference cannot be excluded from the checklists.

**Structural limit:** the family rule leaves most pairs with a single eligible judge (Neon vs Opus: Sol only; Neon vs Sol and Neon vs Terra: Opus only on general tasks; on Ukrainian tasks Neon's output is judged by Sol only and Sol's and Terra's by Neon only). Results are reported per judge, never averaged across judges that saw different candidate sets; a single judge's preference is a model opinion, not a measurement. Judge leniency is anchored by the degraded control and by the Sol–Terra pair.

## 6. Analysis and allowed claims

Per task and judge: rank and 0–16 total. Per arm and suite: mean per judge; keyed accuracy by class and condition with paired closed-book/Sources differences per model (exact McNemar on items only where discordant pairs exist); pairwise preference counts with an exact sign test only where a pair has enough tasks. With 8 general and 6 Ukrainian-judged bundles the design detects only large, consistent gaps; keyed suites at ceiling are reported as ties. "Strong" or "weak" needs the same direction in the mechanical evidence and in every eligible judge; everything else is labelled exploratory. Time, tool calls and cost are reported per arm.

## 7. Success and stop criteria

The study is *valid* only if: at least 99 of 104 generations return a complete, parseable answer (a pre-session infrastructure failure may be retried once and is recorded; a content failure is kept as a score); the degraded control ranks last in at least 90% of judge calls and its mean total is below 60% of the lowest real arm's mean; every judge reply parses to the schema (one retry for invalid JSON, recorded; an unparseable second reply excludes that call and is reported); recorded model identity matches the requested one for every call; and the audit of dispatcher records (section 10) shows no Sources call on any closed-book prompt, no egress outside the provider's own hosts and no write attempt that succeeded. Stream-level file-read audit is not available for these routes, so file isolation rests on the custody measures in section 10. **Stop and report** if any of these fail in a way a retry cannot fix, if cumulative list-price equivalent passes $60 (about twice the estimate), if no dispatch may start after 23:15Z, or if a custody breach is found (the affected arm's results are voided, not patched). A harness bug found by a control is fixed and every arm is rerun on the affected cells. All attempts are preserved; nothing is selected after seeing a score.

## 8. Canaries (2026-11-20, same path as the full run)

**Canary 1** (three-part task: 7 keyed Ukrainian items, a 90–130 word English note from a packet with a planted contradiction and gap, a 30–50 word A2 Ukrainian text), judged in blind cross-family bundles with a degraded control. **Canary 2** (the same 7 keyed items in the final answers-then-explanations format, both conditions, all four arms, opaque task IDs, Neon first, key directories locked). Both are harness checks, not results; their tasks are outside the held-out set.

| Check | Observed |
|---|---|
| Requested model identity | all task records show the requested model and effort high (canary 1: 4 of 4; canary 2: 8 of 8); effective effort not attested |
| Output captured intact | 12 of 12 candidate replies parsed |
| Keyed scorer | every arm scored full marks in both conditions (ceiling by design); wrong answers scored 0 in the negative control |
| Closed-book audit | **0 Sources calls in 4 of 4 closed-book runs**; the Sources condition recorded 16 (Neon), 9 (Sol), 8 (Terra) and 3 (Opus) calls, so the condition is auditable per task |
| Mechanical checks (canary 1) | note lengths 105, 94, 97, 108 words (limit 90–130); Ukrainian text 39, 36, 30, 32 words (limit 30–50); `check_text`: 0 VESUM problems, 0 shadow hits; one UA-GEC suspicion on a Sol verb («виходить»), not a verdict |
| Judges returned the schema | 5 of 5 valid JSON |
| Degraded control | ranked last by 5 of 5 judges (totals 3–7 of 16 against 11–16 for real answers) |
| Blindness audit | all five judges answered "unknown" for every family; a refusal to guess is weak evidence |
| Judge sensitivity | 2 of 3 general judges penalised Opus for an unrequested "treat 15 minutes as the safe limit" recommendation as unsupported; the judges disagreed on the order of the other three arms (one task, no inference) |
| Custody rehearsal and isolation probe | The evaluator restricted candidate and judge access to key material and unblinding maps, and rehearsed the procedure in canary 2. A probe showed the file isolation of the Neon route is weaker than the Codex and Claude routes', so access by a Neon session could not be fully excluded; this is recorded as a limitation |
| Harness defects found and fixed | a scorer bug scoring an empty answer as correct for single-letter keys given as strings (caught by the empty-answer control, fixed, all six sets rerun); decoy cloned one fixed arm and flattened Markdown (rotated, block-aware); apostrophe variants normalised |

Canary 1 list-price equivalents (one call each): Neon $0.349, Opus $0.468, Sol $0.114, Terra $0.046; judge calls: Opus $0.093, Neon $0.266–0.271, Sol $0.037–0.084. Neon's calls took 112–135 s against 25–54 s for the others; with Sources, Neon canary 2 took 67 s against 22–40 s.

## 9. Run size and expected cost

**140 dispatches** (104 generations + 36 judge calls), plus mechanical scoring. Wall time about 70–90 minutes with at most six calls in flight and all Neon generations first.

List prices per million tokens (input / cached or cache-read / output), checked 2026-11-20: Neon $3 / $0.15 / $15 ([Google](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-neon/)); Opus 5.5 $4 / $0.20 / $20 with $8 for one-hour cache writes ([Anthropic](https://platform.claude.com/docs/en/about-claude/pricing)); Sol 6.2 $2 / $0.10 / $10 and Terra 6.1 $1 / $0.05 / $5 (catalog). All routes are subscriptions, so these are equivalents, not charges. Estimates scale each model's canary call by an assumed output size (thinking tokens assumed 8–12k Neon, 3k Opus, 1.5k Sol, 1k Terra) and price Sources-condition calls like the canary calls; treat each figure as ±50%.

| Model | As candidate (26 generations) | As judge | Total |
|---|---|---|---|
| Neon | $9.9 | $3.8 (14 calls) | $13.7 |
| Opus 5.5 | up to $13 (cold cache; less when warm) | $0.7 (8 calls) | up to $13.7 |
| Sol 6.2 | $3.4 | $0.9 (14 calls) | $4.3 |
| Terra 6.1 | $1.4 | none | $1.4 |
| **Total** | **$27.7** | **$5.4** | **about $33** |

## 10. Custody, audit and limitations

Access control for key material and unblinding maps was handled by the evaluator through restricted access, opaque task identifiers and run ordering. One candidate route (Neon) has weaker file isolation than the others, so access by a Neon session to evaluation artifacts could not be fully excluded; no stream-level file-read audit exists for these routes. Post-run audit from dispatcher records covers per-task Sources call counts (zero required on every closed-book prompt), egress destinations and sandbox write-probe results; the judges' family guesses are tallied as a blindness signal. The canaries show no sign of unauthorised access.

Keys, checklists, raw outputs, judge replies, unblinding maps and traces stay in evaluator-held artifacts outside Git; the public report will contain task descriptions, hashes, aggregate scores, class tables and cost, not prompts or keys. Subscription quota is shared with other work, so meters are sampled but cannot attribute spend per arm.

Frozen before the first full-run call: key set `4bb9921904bdd65b9bc26dc25695ade0ae662314c7558c5cb138e86281ecde91`; freeze manifest (all prompts, checklists, keys, class maps, evidence files, preamble, judge template, harness) `a20fb37e05d1a6df6bcd316690bdf036d976a3f14486cf7249a29bdcd494d8c5`; harness `371f884991f5e1a5312fa052387562e5717a0c5a717cbfad34d95de24d6b36f6`; judge template `f31bbb810febcf234e07b858b96fe5066da003cfbccecd8de20b3fabd3869715`; shared preamble `183f1baea8e92d55fa0f105e613a4b04d7712f5f8376356355a32b120bd04801`.

## 11. Authorization

The full run needs a present-tense GO from `claude-cto` on Fleet Comms. Until then only the canaries and one isolation probe have run. To land the report in this lineage the GO must arrive by 22:30Z; at this run size landing is still unlikely before the 23:59Z hand-off (results about 23:45Z, then review, CI and queue), so the plan expects a hash-recorded hand-off for review and landing unless a continuation lineage is authorised.
