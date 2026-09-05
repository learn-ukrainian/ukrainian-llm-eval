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
  --provenance runtime/gec-provenance.json \
  --questions runtime/gec-questions.json --key runtime/gec-key.json
```

Use `--source-sha256` to enforce an expected downloaded-file hash. The preparation
retains all content sentences and both reference annotators, removes the pinned
generator's document headings, and binds the private references to the packet.
Both destinations must be new files. The candidate packet contains only opaque
IDs and source sentences. Preparation alone does not execute or score corrections;
see `scorer/README.md` for the isolated scorer runtime.
