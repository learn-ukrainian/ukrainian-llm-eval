# UA-GEC scorer runtime

This isolated runtime implements the pinned UNLP 2023 scoring sequence: Ukrainian
Stanza tokenization, ERRANT edit extraction, and span-correction F0.5. It supports
the public UA-GEC references with both annotators. It does not replace missing
references or a failed scorer with approximate string matching.

The scorer image runs Python 3.11 as an unprivileged user. Build the Linux amd64
image with Docker (Apple Silicon requires amd64 emulation):

```bash
docker build --platform linux/amd64 -t ukrainian-llm-eval-scorer:locked scorer
```

The base image is digest pinned, every Python wheel in `requirements.lock`
(including the PyTorch CPU wheel) is installed with `--only-binary=:all: --no-deps --require-hashes`,
and the English spaCy model archive in `model.lock` is installed offline with
`--no-index --no-build-isolation --no-deps --require-hashes` using the hash-verified
`setuptools` and `wheel` already installed in the image, followed by `pip check`.
`resources.json` pins the installed tokenizer resources by file hash. Direct Wolfi
`apk` packages are version-pinned; their transitive system packages still resolve
from the live Wolfi index. Build requires network access. The build fails if any
package hash, dependency closure, or tokenizer resource differs from the recorded
hashes. Record the resulting immutable image ID for each experiment.

Run the six synthetic parity cases against the hash-verified upstream script:

```bash
.venv/bin/python scorer/check_parity.py --image ukrainian-llm-eval-scorer:locked \
  --output runtime/scorer-parity.json
```

This checker downloads the upstream script, then runs both scorers with networking
disabled. It compares unchanged, replacement, wrong replacement, insertion,
deletion and multiple-reference cases using pretokenized input. The replacement
case also runs with default tokenization and must produce the same result. Parity
coverage is limited to these cases and the pinned tokenizer build.
The output must be a new file so earlier parity evidence is preserved.

For actual corrections, supply one sentence per line in the same order as the
reference content sentences. Exclude generated document-heading blocks from the
scoring references while preserving all content blocks and both annotators.
Keep private inputs behind an owner-only parent directory (mode `0700`).
Mount only a readable child directory (mode `0755` with input files mode `0444`)
and capture JSON metrics outside candidate access. Use an immutable image ID
in place of `IMAGE_ID`:

```bash
docker run --rm --platform linux/amd64 --network none --read-only --tmpfs /tmp \
  -v /absolute/private/inputs:/inputs:ro IMAGE_ID \
  --corrected /inputs/corrected.txt --references /inputs/references.m2
```

Add `--no-tokenize` only for already tokenized corrections. Normal scoring uses
installed resources with downloads disabled. Each invocation uses a separate
temporary directory. Missing resources, a line-count mismatch, or a failed scoring
subprocess is an error. The JSON includes input byte hashes and the sentence count;
retain it with the run, reference, image and parity identities in private evidence.

No real benchmark data or model credentials are included in this directory.
