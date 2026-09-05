> Development runbook: these commands describe the extracted exam engine. The complete three-suite public evaluation, evidence archives and additional adapters remain tracked release work. Historical pilot notes are not validation of this package revision.

# ZNO/NMT model and agent evaluation

Run an exam under two conditions: **closed-book** (no tools) and
**Sources-assisted** (only selected reference tools). The evaluator separates
question preparation, candidate execution and answer-key scoring. It does not
require the LU corpus for closed-book runs or offline scoring.

The software is covered by the repository [MIT license](../LICENSE).
Exam datasets and reference corpora retain their own rights. Do not infer a
dataset license from the software license.

## What “blind” means here

The candidate receives only a validated question packet. Its input has no gold
answers, explanations, source URLs or scoring metadata. A separate scoring
command reads the key after the candidate has submitted its answers.

The native adapter restricts the candidate's tool surface; the HTTP adapter
executes only explicitly permitted reference calls. These are controls on the
model's access through a trusted harness, **not an operating-system sandbox
against a malicious CLI binary, provider or operator**. Keep the grader/key
custodian separate from any untrusted execution host.

Claude trials record the native CLI's terminal session ID and reject missing or
inconsistent session evidence. This lets the research scheduler detect reused
sessions. Requested effort remains distinct from effective effort when the CLI
does not attest the latter.

Published past papers may have appeared in model training. The term
`public-exposure-possible` records that uncertainty; hiding the key cannot
prove the questions were unseen. Private questions require independent authoring
and review, separate custody, corpus-overlap checks and a frozen test set that
has not been used for prompt tuning. This runner does not manufacture such
evidence or certify a private test's provenance.

The Sources corpus must not contain private held-out questions or their keys.
For public papers, reference search can potentially retrieve the published
question or its solution. Treat that as possible contamination, not proof of
reasoning. An MCP schema hash proves a tool interface, not corpus cleanliness.

## Installation and runtime storage

Install the package using the root README. Native Claude runs require an authenticated Claude CLI. Compatible HTTP runs use an explicitly configured endpoint. Offline preparation and scoring need neither provider access nor MCP.

For reference-assisted runs, set `SOURCES_MCP_URL` privately to an authorized compatible MCP endpoint. The server must expose the configured reference tools. The evaluator does not deploy a server or include a corpus. Closed-book runs need no Sources endpoint.

Create a private runtime directory. All following commands assume you remain
at the worktree root:

```bash
umask 077
mkdir -p .runtime/zno-nmt-demo
```

Runtime output files are created exclusively with owner-only permissions.
Existing trials are never overwritten. Keep source exams, question packets,
keys, private retrievals and raw candidate results out of Git even if you use
a different directory. Do not publish an entire runtime directory.

## Prepare an exam

The normalized input format is `zno-nmt.exam.v1`. The following is a **synthetic
format example**, not an exam-quality benchmark or language evaluation. Save
it as `.runtime/zno-nmt-demo/exam.json`:

```json
{
  "schema": "zno-nmt.exam.v1",
  "title": "Synthetic format demonstration",
  "subject": "synthetic",
  "year": 2026,
  "provenance": {
    "source_url": "https://example.org/synthetic",
    "source_revision": "example-v1",
    "license": "synthetic-example",
    "exposure": "public-exposure-possible"
  },
  "scoring": {
    "kind": "benchmark",
    "policy_url": null,
    "pass_threshold": null,
    "expected_items": 1,
    "expected_points": 1
  },
  "items": [{
    "id": "1",
    "kind": "single",
    "question": "Select the option whose text is exactly X.",
    "options": [{"id": "A", "text": "X"}, {"id": "B", "text": "Y"}],
    "rows": [],
    "correct": "A"
  }]
}
```

For a matching item, use `kind: "matching"`, numbered `rows`, lettered
`options`, and a `correct` object such as `{"1":"B","2":"A"}`. Empty row
text is permitted when the numbered targets already appear in the question.
Preserve order, Unicode punctuation, stress marks and meaningful emphasis;
Markdown can represent emphasis from the source paper. Unsupported task types
are rejected, never silently removed.

```bash
.venv/bin/python -m ukrainian_llm_eval prepare \
  --exam .runtime/zno-nmt-demo/exam.json \
  --questions .runtime/zno-nmt-demo/questions.json \
  --key .runtime/zno-nmt-demo/grading-key.json
```

Give the candidate runner only `questions.json`. The key remains with the
grader. The packet and key have independent digests and binding checks.

### Import a public NLPForUA/ZNO paper

Use a pinned upstream revision, inspect the upstream data license, and retain
the source file's SHA-256. Do not download a mutable `main` as a frozen release.
The importer accepts the upstream array of complete papers:

```bash
.venv/bin/python -m ukrainian_llm_eval import-zno \
  --input .runtime/zno-nmt-demo/upstream.json \
  --test-id 515 \
  --metadata .runtime/zno-nmt-demo/metadata.json \
  --output .runtime/zno-nmt-demo/exam.json
```

`metadata.json` contains the normalized example's `title`, `subject`, `year`,
`provenance` and `scoring` fields only. Upstream task IDs are zero-based; the
importer maps them to one-based paper numbers, then preparation replaces them
with opaque candidate IDs. Matching headers are retained as row/column IDs.
Image-dependent or malformed items block the complete import.

The initial verification paper is `test_id="515"` from
[NLPForUA/ZNO at revision 35d4c976](https://github.com/NLPForUA/ZNO/tree/35d4c976599243b08dcd04303b557a7c2dc86068).
It corresponds to the Ukrainian-language block of the
[official 2022 demonstration paper](https://testportal.gov.ua/wp-content/uploads/2022/04/NMT_2022_demonstratsijnyj-variant_maket.pdf).
That PDF's SHA-256 is
`4c1dc23303ba919a71eb28210dc04fb4abb472b8d3d288fd63141bfdfd9dc154`.
The block contains 15 single-choice and five four-pair matching items: 20 items,
35 possible points. The [official 2022 FAQ](https://testportal.gov.ua/zapytannya-vidpovidi-2/)
documents scoring and the one-raw-point minimum subject threshold. Crossing
that minimum is weak evidence; report raw points and mistakes prominently.
This is a subject-block simulation, not certification that a model passed the
entire multi-subject NMT or can author curriculum reliably.

Upstream plain text loses some PDF emphasis. Independently compare every
question to the official paper and restore meaningful emphasis in a separately
versioned runtime representation before claiming paper fidelity. Record both
source and presentation hashes. Never tune the representation using a model's
score. ZNO-Eval v2 was still marked
[release in progress](https://huggingface.co/datasets/NLPForUA/zno-eval-v2)
when this workflow was prepared; it is not an implicit dependency.

## Offline scoring smoke test

You can verify the installation without provider access using the synthetic one-question example above. This is a **hand-authored fixture**, not evidence that any model took an exam. Never relabel it as a real candidate run. A score validates answers and packet binding; it does not authenticate a provider execution.

After `prepare`, create `.runtime/zno-nmt-demo/manual-fixture.json` with the following script. The required run envelope contains `schema`, `packet_sha256`, `condition`, `status`, `responses`, `identity`, `comparison` and `metrics`. For actual experiments, use the runner-produced envelope and retain its identity/metrics rather than constructing them manually.

```sh
.venv/bin/python - <<'PYCODE'
import json
from pathlib import Path

root = Path(".runtime/zno-nmt-demo")
packet = json.loads((root / "questions.json").read_text())
fixture = {
    "schema": "zno-nmt.run.v1",
    "packet_sha256": packet["packet_sha256"],
    "condition": "closed-book",
    "status": "ok",
    "responses": {"q0001": "A"},
    "identity": {"model": "synthetic-manual-fixture", "harness": "manual-fixture"},
    "comparison": {},
    "metrics": {}
}
with (root / "manual-fixture.json").open("x") as stream:
    json.dump(fixture, stream)
PYCODE
.venv/bin/python -m ukrainian_llm_eval score \
  --questions .runtime/zno-nmt-demo/questions.json \
  --key .runtime/zno-nmt-demo/grading-key.json \
  --run .runtime/zno-nmt-demo/manual-fixture.json \
  --output .runtime/zno-nmt-demo/manual-score.json
```

Expected result: `raw_points: 1`, `max_points: 1`, `correct_items: 1`, and `passed: null` because this synthetic benchmark has no official passing threshold. For a negative integrity check, copy the question packet, change its question text without changing its stored digest, and score that copy to a new output path. Expected result: exit `2`, a sanitized `ExamError`, and no new score file. This proves tamper detection, not Ukrainian language competence.

## Configure a candidate

Save a configuration as `.runtime/zno-nmt-demo/config.json`. Pin a concrete
model ID available through your authenticated CLI instead of a moving alias:

```json
{
  "schema": "zno-nmt.config.v1",
  "adapter": "claude",
  "provider": "anthropic",
  "model": "REPLACE_WITH_CONCRETE_MODEL_ID",
  "effort": "high",
  "timeout_seconds": 600,
  "max_output_tokens": 32768,
  "max_tool_calls": 20,
  "repeats": 3,
  "tools": ["verify_word", "verify_stress", "search_text", "query_pravopys"],
  "corpus_id": "REPLACE_WITH_FROZEN_CORPUS_ID"
}
```

The same configuration is used for both conditions. `tools` defines the
assisted condition only. Closed-book execution exposes none of them. Do not
put endpoint URLs, tokens or private machine details in a checked-in config.
An unsupported effort setting must not silently become a different setting.
Requested effort and proven effective effort are different facts; absent
runtime evidence stays unknown.

For native Claude, a `[1m]` context selector remains the requested model
string. The effective model may be its canonical base name only when the
initial stream announces that exact selector and terminal `modelUsage`
contains exactly that selector, its matching `canonicalModel`, and a
1,000,000-token context window. Missing or contradictory metadata still fails
identity validation; the adapter does not infer this mapping by stripping
the suffix alone.

Freeze budgets before the scored experiment. Reasoning can consume the native
output budget even when the requested final JSON is short: the complete demo
paper exceeded a 4,096-token budget in live verification. Preserve such failures;
changing the budget starts a new experiment, not a replacement successful trial.

## Run without MCP

```bash
.venv/bin/python -m ukrainian_llm_eval preflight \
  --config .runtime/zno-nmt-demo/config.json --condition closed-book
.venv/bin/python -m ukrainian_llm_eval run \
  --questions .runtime/zno-nmt-demo/questions.json \
  --config .runtime/zno-nmt-demo/config.json \
  --condition closed-book --output .runtime/zno-nmt-demo/closed-book.json
```

No Sources endpoint is needed for this standalone condition. The native
adapter uses a fresh external directory, disables built-in tools, skills and
ambient settings, and supplies an empty strict MCP configuration. It does not
reuse the current coding task's conversation.

## Run with Sources MCP

```bash
.venv/bin/python -m ukrainian_llm_eval preflight \
  --config .runtime/zno-nmt-demo/config.json --condition sources
.venv/bin/python -m ukrainian_llm_eval run \
  --questions .runtime/zno-nmt-demo/questions.json \
  --config .runtime/zno-nmt-demo/config.json \
  --condition sources --output .runtime/zno-nmt-demo/sources.json
```

Reference calls run through an allowlisted bridge. General browsing, local
files and shell tools are not exposed. Available access and actual tool use
are reported separately: an agent may choose not to use its references.
The assisted prompt states the configured total call limit, including failed
attempts, so the candidate can allocate its references before hitting the cap.
Exceeding that limit is reported separately from attempting a forbidden tool.
Time and tool-call limits apply to candidate execution; capability probes add
startup time outside the recorded native execution duration and timeout.
Native cost is the CLI-reported estimate, not a subscription billing receipt;
reported input tokens may exclude separately accounted cache tokens.
Missing corpus identity remains a limitation,
not an invented version. A configured corpus label alone is not proof that the
underlying corpus stayed unchanged.

For the paired experiment, prefer the following single command. It freezes
the schedule before execution, checks both conditions, alternates their order
across repeats, and creates a fresh session for every trial:

```bash
.venv/bin/python -m ukrainian_llm_eval pair \
  --questions .runtime/zno-nmt-demo/questions.json \
  --config .runtime/zno-nmt-demo/config.json \
  --out-dir .runtime/zno-nmt-demo/paired
```

A failed trial stops the schedule and remains recorded. Do not overwrite it
with a successful retry, select only the best run, or change prompts based on
the scored paper. A deliberately new experiment needs a new output directory
and its own declared configuration. API errors, malformed output and tool
violations are failures, not silently repaired answers.

## Score independently and compare

These commands do not invoke a model or MCP. Run them under the grader's
custody after answers are frozen:

```bash
.venv/bin/python -m ukrainian_llm_eval score \
  --questions .runtime/zno-nmt-demo/questions.json \
  --key .runtime/zno-nmt-demo/grading-key.json \
  --run .runtime/zno-nmt-demo/paired/001-closed-book.json \
  --output .runtime/zno-nmt-demo/closed-book-score.json
.venv/bin/python -m ukrainian_llm_eval compare \
  --questions .runtime/zno-nmt-demo/questions.json \
  --key .runtime/zno-nmt-demo/grading-key.json \
  --control .runtime/zno-nmt-demo/paired/001-closed-book.json \
  --treatment .runtime/zno-nmt-demo/paired/001-sources.json \
  --output .runtime/zno-nmt-demo/comparison.json
```

Single-choice items earn one point for the correct choice. Matching items earn
one point per correct pair independently; repeated selected columns are flagged
without erasing a correct pair. Missing/invalid answers stay in the denominator. A failed execution is
not a passing exam. `official` scoring needs a policy URL, explicit threshold
and complete expected item/point counts; a `benchmark` score has no pass claim.
For other subjects, years or response types, verify the applicable official
rules before selecting a policy. This implementation does not score essays,
numeric free responses, ordering tasks or images.

Comparisons reject mismatched datasets or generation settings. The output
contains paired item wins/losses/ties and a point difference, not a statistically
significant general ranking. Compare every repeat and report variability;
one paper from one model does not establish a community leaderboard.

## Share results without sharing the corpus

```bash
.venv/bin/python -m ukrainian_llm_eval export \
  --input .runtime/zno-nmt-demo/comparison.json \
  --output .runtime/zno-nmt-demo/public-aggregate.json
```

The export command selects numeric aggregate fields; it never copies arbitrary
metadata, exam text, answer values, paths, retrieval text or provider transcripts.
Inspect any additional narrative before publication. Share the model/version,
requested and verified effort, harness version, dataset and scoring policy,
prompt hash, repeat count, tool allowlist, non-sensitive corpus version and
limitations separately. Do not publish private retrieval snippets to make a
result look more reproducible.

Exact public-paper reproduction requires the same accessible model and verified
presentation packet; the raw upstream text alone does not reproduce restored
typography. LU Sources-assisted results depend on a private corpus and
must say so. Community users can substitute their own corpus/MCP and report
that as a distinct treatment, not the same benchmark condition.

## Adapter capability boundary

| Adapter | Closed-book | Reference tools | Model/effort evidence |
| --- | --- | --- | --- |
| Native Claude CLI | Strict empty MCP and built-in tool set | Filtered stdio-to-HTTP Sources bridge | CLI output checked; unreported effective effort remains unknown |
| Chat-completions HTTP | No tools supplied | Controller executes allowed MCP function calls | Exact returned model required; effective effort may remain unknown |
| Other agent harnesses, including native Codex | Not claimed by this version | Requires an independently verified adapter | Do not relabel an imported answer file as a verified isolated run |

HTTP configuration uses `adapter: "chat-http"` and environment-variable names
`endpoint_env` and `key_env`; populate their values privately. The adapter
implements a narrow chat-completions protocol, not every provider API. Local
model endpoints can use it; model-specific effort support still needs a
route-specific canary. Use only your own authorized subscriptions or credentials. Configuring an endpoint does not establish entitlement or authorize extra spending.

For endpoints that support JSON-object mode but not JSON Schema, set
`http_response_format: "json_object"`. The evaluator still validates the
returned answer IDs and structure locally; this setting changes the provider
request format, not the scoring contract. The default remains `json_schema`.

An OpenRouter route can freeze these explicit controls:

```json
"openrouter": {
  "provider_endpoint": "<provider-endpoint-slug>",
  "expected_provider_name": "<provider-name-returned-in-responses>",
  "reasoning_enabled": false
}
```

The adapter sends a single-provider `only` list, disables fallback, requires
parameter support, and rejects missing or different response provider names
on every completion round. With reasoning disabled, `effort` must be null.
With reasoning enabled, a configured effort is sent in `reasoning.effort`.
Both settings and the observed provider name remain in the evidence.

Resolve the endpoint slug and expected response name from current provider
evidence before freezing the route. OpenRouter base slugs may cover multiple
endpoint variants; do not mistake a provider name for independent attestation
of a particular variant. Pricing and capability admission must cover every
backend the selected slug permits. See the official
[provider routing contract](https://openrouter.ai/docs/guides/routing/provider-selection).
These controls do not themselves prove entitlement, token bounds, billing
settlement, or effective reasoning effort.

For a paid text route, also set `openrouter.max_price` to an object containing
`prompt`, `completion`, and `request`. All three values must be nonnegative
decimal strings, for example `{"prompt": "0.10", "completion": "0.34", "request": "0"}`.
Prompt and completion ceilings are USD per million tokens; the request ceiling
is USD per request. The adapter forwards these ceilings on every completion
round without relaxing provider restrictions. Set them consistently with the
frozen spending reservation. A price ceiling filters providers; the shared
ledger still enforces the total spending cap and retains uncertain charges.

## Validation and contribution

```bash
.venv/bin/python -m pytest tests/eval/test_zno_nmt*.py
.venv/bin/ruff check src/ukrainian_llm_eval tests/eval/test_zno_nmt*.py
```

The tests exercise packet/key tampering, malformed response structures,
partial credit, denominator retention, comparison drift, denied tool calls,
runtime limits and privacy-safe export. Mocked adapter tests prove mechanics,
not live provider support. A release needs separate full-paper paired live
evidence and independent review of the exact implementation revision.

Useful community contributions include independently audited exam importers,
provider adapters with isolation evidence, public-corpus reference baselines,
and new privately stewarded questions. Keep dataset preparation, prompt tuning
and held-out scoring separate so improvements remain measurable.


### Evidence capture boundary

The Python runner accepts an optional synchronous `evidence(kind, payload)` callback.
It emits validated trial inputs, preflight receipts, the rendered prompt and response
schema, provider responses before validation, and tool requests/results. Native CLI
completion and timeout events include captured stdout/stderr, including partial
output recovered when the process group is stopped. The callback must copy or
serialize each payload before returning: HTTP conversation lists change during a run.

These events are private raw evidence, not sanitized publication artifacts. The
controller does not pass transport authorization headers, environment contents, or
endpoint URLs to the callback. Provider or retrieved text can still contain sensitive
content; this is not a guarantee that arbitrary raw payloads are secret-free. Keep
raw evidence outside Git and review it before any publication. The aggregate export
command continues to exclude raw text.

The callback cannot recover output from an abrupt machine failure or a process
killed before buffered output reaches the controller. Such an attempt must remain
incomplete; do not report it as a successful or unattempted trial.

### Private attempt storage and resume

`run` creates an owner-only evidence directory next to its output (for example,
`trial.evidence/` for `trial.json`) and a verified receipt at
`trial.evidence.json`. Override the directory with `--evidence-dir PATH`.
`pair` stores all attempts under `OUT_DIR/evidence/`, with one immutable receipt
for each scheduled cell. Do not place these directories in Git.

Resume a paired plan with the same questions and configuration:

```bash
ukrainian-llm-eval pair --questions questions.json --config config.json \
  --out-dir paired-run --resume
```

Resume checks the frozen plan and existing evidence before reusing results. It
never reruns a started cell. An interrupted cell retains its recorded events and
is finalized as an interrupted failure; only cells that never started may make
new provider calls. A newly failed trial stops execution; a deliberate resume can
continue later cells while retaining that failure. The command exits 2 whenever
any visited cell failed, including a failure retained from an earlier invocation.

The schedule uses a POSIX file lock to exclude concurrent cooperating executors.
Treat the evidence directory and its parent directories as trusted local storage.
Hashes detect corruption against retained receipts; they do not prevent a local
owner from rewriting an entire history and all its hashes. A crash during an event
write may leave a truncated log: verification then fails closed and preserves it
for investigation instead of inventing a complete record.

Resume also binds hashes of the resolved completion and MCP endpoints. Changing
an endpoint between invocations fails before preflight or a new provider call;
rotating credentials for the same endpoint does not change that route identity.

Inspect every attempt even when one is damaged:

```bash
ukrainian-llm-eval evidence-status --evidence-dir paired-run/evidence \
  --output private-evidence-status.json
```

This command makes no provider calls. The private report includes each intact
receipt and a corruption marker for unreadable entries; stdout contains counts
only. Exit 2 means at least one entry is incomplete or corrupt. Paid resume remains
blocked if any evidence is corrupt. Preserve the original directory, use this
report to identify the affected entry, and investigate against retained copies or
checksums. Do not delete a damaged entry to make resume pass or claim its cell was
never attempted. A recovery that cannot establish the original evidence remains
an explicit experiment gap.

Evidence storage requires POSIX ownership and locking support. Windows is not
supported for evidence-backed execution.

## Share a spending cap across research runs

For provider-bound paid execution, include the explicit `spending_policy` in the
`plan-research` specification described in [request budgets](request-budget.md).
Pass one absolute path to `run-research --shared-spending-ledger /private/runtime/spending.sqlite`.
Use that same file for canaries and every scored run under the authorization;
keep it outside all execution roots. A different output directory does not reset
the cap when the ledger is reused. Creating a different ledger would create an
independent budget and must not be used to bypass the authorization.

The plan retains all scheduled cells even when their combined worst-case cost
exceeds the cap. Execution reserves each segment before sending a request and
releases unused funds only against authoritative final account charges. A budget
stop exits 2, preserves the remaining denominator in `budget-stop.json`, and
prevents a primary score report. It is an incomplete experiment, not a smaller
successful benchmark. The original upfront-reservation mode remains available
for existing version 1 plans.
