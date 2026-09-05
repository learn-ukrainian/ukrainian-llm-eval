# Ukrainian benchmark protocol

`benchmarks/sources.json` records the intended public experiment sources, exact
upstream revisions, denominators and declared licenses. It is a source inventory,
not approval to execute a scored batch. Before execution, imported packets,
private grading references, fidelity receipts and scorer identities must be bound
by hashes in the experiment manifest.

Report the three suites separately:

| Suite | Full denominator | Primary measure |
| --- | --- | --- |
| NMT 2022 demonstration Ukrainian language block | 20 tasks, 35 points | Official raw points |
| ULP | 347 questions | Exact-option accuracy |
| UA-GEC public `gec-only/test` | 2,696 sentences, 166 documents | Span-correction F0.5 |

Do not average these measures into an overall language score. These tasks do not
establish general fluency or a CEFR level. Public availability also means training
contamination cannot be ruled out. “Blind” describes withholding grading references
from the candidate during execution; it does not prove an unseen training set.

## Source and reference custody

The public repository contains code, synthetic tests and source metadata. Download
real benchmark content into private runtime storage. Keep correct options, M2
references and scoring commands outside candidate-accessible directories and tools.
Record source-byte hashes before normalization and packet hashes afterwards. A
normalization or typography correction needs its own traceable verification receipt.

Dataset licenses remain separate from the package's MIT license. NMT data is
CC BY-NC 4.0, ULP is declared MIT by its publisher, and UA-GEC is CC BY 4.0. Preserve
attribution when distributing any permitted data or result artifacts. Normative
reference books mentioned by a dataset are not bundled in this project.

## UA-GEC source denominator

The pinned source contains 2,696 sentences and 43,603 tokens in 166 documents.
The upstream README still reports 2,704 sentences and 43,605 tokens. Use the
actual pinned source files, not that stale table: the M2 file contains 2,862
blocks, comprising 166 generated document headings and all 2,696 content blocks.
The content lines match the official tokenized source files in order. Upstream
sentence-boundary changes account for the net eight-sentence difference; no
content sentence is dropped to obtain this denominator. Preserve both annotators.

## GEC scorer equivalence

The pinned official shared-task script tokenizes with Stanza Ukrainian, derives
edits through ERRANT, then invokes `errant_compare`. Use the full public UA-GEC
`gec-only/test` split and both reference annotators. The hidden UNLP 2023 test and
local filtered derivatives are different benchmarks.

Install scorer dependencies and tokenizer resources in a separate environment;
record package versions and resource hashes. Scoring must preflight these resources
and must not download them during an experiment. The upstream script uses fixed
names under the temporary directory: each invocation therefore needs its own
`TMPDIR`, preventing concurrent evaluations from sharing intermediate files.

Before accepting real scores, compare the package wrapper with the pinned official
scorer using synthetic unchanged, exact/wrong replacement, insertion, deletion and
multiple-reference cases. A missing dependency, incomplete denominator or parity
failure blocks scored execution; it is not permission to substitute a convenient
string-distance metric.

## Live Sources condition

Use the same packet and model/effort settings for paired conditions, changing only
the approved tool surface. Keep tool calls and responses as private run evidence.
Exclude direct benchmark-answer or correction-example lookup. Live Sources may
still overlap with public questions; disclose this and record observed service
identity without claiming a frozen corpus snapshot. Corpus publication and backups
are separate work and are not prerequisites for this tool's optional MCP mode.

## Import ULP

Save the pinned upstream JSONL file outside Git. Create metadata using the normal
exam metadata fields (`title`, `subject`, `year`, `provenance`, `scoring`); for the
full ULP profile set both `expected_items` and `expected_points` to 347, scoring
kind to `benchmark`, and pass threshold to null. Record the declared MIT license
and pinned source revision from `benchmarks/sources.json`.

```bash
ukrainian-llm-eval import-ulp --input runtime/ulp.jsonl \
  --metadata runtime/ulp-metadata.json --output runtime/ulp-exam.json \
  --sidecar runtime/ulp-source-receipt.json \
  --source-sha256 fee7ea2131bf4ea1d576ec356bf9c6b616b0bf1e31fd0ebf014371105ea3ede0
ukrainian-llm-eval prepare --exam runtime/ulp-exam.json \
  --questions runtime/ulp-questions.json --key runtime/ulp-key.json
```

The private sidecar binds original source bytes, normalized exam and candidate
packet hashes, and retains category/source IDs for analysis. It contains no
correct options but should still stay outside candidate access. The generic
importer supports small synthetic fixtures; accepting a fixture does not admit it
to the 347-question public experiment. Source rows must contain 3–5 choices and
consistent zero-based answer index and Ukrainian answer letter.

## Prepare UA-GEC references

Download the pinned full M2 file into private storage. Supply a provenance JSON
object containing `source_url`, `source_revision`, `license` and `exposure`.

```bash
ukrainian-llm-eval prepare-gec --input runtime/test.m2 \
  --provenance runtime/gec-provenance.json --expected-sentences 2696 --expected-documents 166 \
  --questions runtime/gec-questions.json --key runtime/gec-key.json
```

Use `--source-sha256` to enforce an expected downloaded-file hash. The preparation
retains all content sentences and both reference annotators, removes the pinned
generator's document headings, and binds the private references to the packet.
For a verified full-corpus preparation, enforce both expected counts; a mismatch
or duplicate document heading fails before writing output. Generic custom M2
inputs may deliberately omit these assertions. Both destinations must be new files. The candidate packet contains only opaque
IDs and source sentences. Preparation alone does not execute or score corrections;
see `scorer/README.md` for the isolated scorer runtime.

## Run and score GEC

The `run` and `pair` commands accept the prepared GEC question packet using the
same provider configuration and evidence controls as exam questions. Reference
keys are accepted only by offline preparation/scoring commands. Each run must
return every sentence ID; no partial subset is presented as a full-corpus score.
Use `run --condition closed-book` for a route with no controlled MCP support.

```bash
ukrainian-llm-eval run --questions runtime/gec-questions.json \
  --config runtime/config.json --condition closed-book \
  --evidence-dir runtime/gec-runs --output runtime/gec-run.json
ukrainian-llm-eval score-gec --questions runtime/gec-questions.json \
  --key runtime/gec-key.json --run-evidence-dir runtime/gec-runs \
  --attempt-id ATTEMPT_ID --scorer-image sha256:IMAGE_DIGEST \
  --evidence-dir runtime/gec-scoring --output runtime/gec-score.json
```

Read the attempt ID from the run's `.evidence.json` receipt. Replace the image
placeholder with the immutable local image ID recorded during scorer provisioning;
mutable tags are rejected. Docker runs with networking disabled, read-only input
mounts and no image pulls. Scoring timeout defaults to 600 seconds; `--timeout`
sets an explicit alternative. A timed-out invocation retains partial output and
attempts to remove its own uniquely named container.

The scoring evidence binds the verified execution receipt, candidate packet,
private reference key, serialized inputs, wrapper implementation and scorer image.
Raw scorer output is retained before parsing. Missing answers or failed candidate
runs produce a failed scoring receipt with no F0.5; answers are not repaired or
silently excluded. Scorer failures also remain recorded. Keep all scoring evidence
private because it includes references. `export` emits only allowlisted numeric
aggregates and cannot replace the private provenance record.

## Restore verified NMT typography

An upstream text transcription can omit meaningful emphasis or matching-table
headings. Audit the transcription against the official paper before constructing
an overlay; this tool does not verify a PDF merely because its hash is supplied.
Keep the reviewed overlay and original paper in private runtime custody.

The overlay schema is `ukrainian-llm-eval.typography.v1`. It contains
`source_exam_sha256` (the core canonical digest of the normalized input exam),
`official_source` with `url` and `sha256`, and an ordered `changes` list. Each
change selects an original exam `item_id` and either:

- `field: "question"`, `original_text`, `replacement_text`: replace one unique
  span, changing only Markdown asterisks while preserving all underlying text.
- `field: "matching_headings"`, `left`, `right`: append the independently verified
  table headings to a matching question, once per item.

```bash
ukrainian-llm-eval apply-typography --exam runtime/nmt-plain.json \
  --overlay runtime/nmt-reviewed-overlay.json --output runtime/nmt-restored.json \
  --receipt runtime/nmt-typography-receipt.json
ukrainian-llm-eval prepare --exam runtime/nmt-restored.json \
  --questions runtime/nmt-questions.json --key runtime/nmt-key.json
```

Both output paths must be new. A source-hash mismatch, ambiguous span, lexical
change or unsupported edit fails. The receipt binds before/after exams and packets,
the overlay and declared official-source hash; it contains no question or key text.
Original option order, matching rows and answers are preserved. Emphasis in a text
prompt is a documented representation of paper typography, not pixel equivalence.

### M2 fixture format

Official UA-GEC files can be supplied directly. For a synthetic plumbing test,
M2 uses an `S ` source line followed by `A ` annotations and a blank block
separator. Annotation fields are separated by three vertical bars:
`start end|||category|||replacement|||REQUIRED|||-NONE-|||annotator_id`.
Offsets are zero-based, with an exclusive end. Both annotators `0` and `1` must
be represented for each sentence. A deletion has an **empty replacement field**;
`-NONE-` is the no-op marker, not the deletion replacement. For example, this
artificial edit tests deleting the second token, without making a grammatical
correctness claim:

```text
S Я дуже люблю чай
A 1 2|||U||||||REQUIRED|||-NONE-|||0
A 1 2|||U||||||REQUIRED|||-NONE-|||1

```

An unchanged reference uses `A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||0`
(and an equivalent line for annotator `1`). Invalid field counts or unsupported
annotator IDs fail with a sanitized input error. Never repair official reference
annotations to improve a model's score.

Manually edited annotation fields must remain on one physical line. Key
validation and M2 serialization reject every Unicode line boundary recognized
by Python's `splitlines`, including vertical tab, next-line, and the Unicode
line and paragraph separators.

Span-correction scores depend on edit alignment as well as corrected text. A
synthetic repeated-token case produced the same corrected text from two possible
deletion spans; ERRANT selected one while the reference specified the other,
resulting in a false-positive/false-negative pair. Preserve official references
and disclose this metric limitation instead of adjusting references after seeing
a model's result.

## Verify complete benchmark artifacts

After preparation, reconstruct the artifacts from the original downloaded bytes
and a selected source profile. This checks exact question/order/key identity and
declared denominators, rather than trusting hashes recomputed from modified input.

```bash
ukrainian-llm-eval verify-benchmark --profiles benchmarks/sources.json \
  --suite nmt-2022-demo-ukrainian --source runtime/nmt-source.json \
  --exam runtime/nmt-restored.json --overlay runtime/nmt-reviewed-overlay.json \
  --questions runtime/nmt-questions.json --key runtime/nmt-key.json \
  --output runtime/nmt-benchmark-manifest.json
```

For ULP, use suite `ulp`, its original JSONL and normalized exam, without an overlay.
For GEC, use `ua-gec-public-gec-only-test`, its original M2, packet and key, with
neither `--exam` nor `--overlay`. `--profile-sha256` can enforce a previously
approved canonical profile digest. The output must be new and remains private.
Manifest `key_sha256` hashes the entire supplied key object; GEC's internal
`key_sha256` field separately hashes its key body and has a different value.

GEC profiles must declare `denominator.sentences`; `documents` and `tokens` are
optional. Verification checks every denominator field that the profile declares
and leaves omitted optional fields out of the manifest, so a small custom M2
fixture can verify sentence coverage without inventing document or token counts.
Unknown fields, malformed counts and source drift fail before the manifest is
created. A successful manifest is also write-once: rerunning the command against
an existing output refuses to replace it and preserves its bytes.

The manifest states `matches_supplied_profile`: a custom profile can intentionally
select a synthetic fixture and gets a different profile identity. It is not a
release-admission certificate or an independent PDF audit. Freeze the reviewed
profile, fidelity receipts, routes, limits and scorer identity before execution.

### Primary research runs versus diagnostics

The `run`/`pair` examples above execute one whole packet per session and are useful
engine or agent-endurance diagnostics. The primary research experiment requires
fresh sessions per complete NMT task, ULP question and GEC document, preserving all
material needed by each unit. Every cell must cover the full fixed dataset; GEC
responses are reassembled in canonical order and scored once against both
annotators. This segmented research scheduler is tracked in issue #3 and is not
yet supplied by the whole-packet `pair` command. Do not label a partial dataset or
a whole-packet endurance run as the primary research protocol.
