"""Offline implementation of the pinned UNLP span-correction scoring protocol.

Runs inside the separately provisioned scorer image, never in a candidate process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path

import spacy
import stanza


def verify_resources():
    import en_core_web_sm
    roots = {"stanza": Path("/opt/stanza"), "spacy-en": Path(en_core_web_sm.__file__).parent}
    manifest = json.loads(Path(__file__).with_name("resources.json").read_text())
    for name, expected in manifest["files"].items():
        label, relative = name.split("/", 1)
        path = roots[label] / relative
        path.resolve().relative_to(roots[label].resolve())
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("scorer resource hash mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corrected", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--no-tokenize", action="store_true")
    args = parser.parse_args()
    verify_resources()
    reference_bytes = args.references.read_bytes()
    corrected_bytes = args.corrected.read_bytes()
    sources = [line[2:] for line in reference_bytes.decode("utf-8").splitlines() if line.startswith("S ")]
    corrections = corrected_bytes.decode("utf-8").splitlines()
    if not sources or len(sources) != len(corrections) or any(not line.strip() for line in corrections):
        raise ValueError("prediction/reference sentence denominator mismatch or empty prediction")
    spacy.load("en")  # Fail here if provisioned resources are missing; never download.
    if not args.no_tokenize:
        nlp = stanza.Pipeline(lang="uk", processors="tokenize", download_method=None, use_gpu=False)
        corrections = [" ".join(token.text for token in nlp(line).iter_tokens()) for line in corrections]
    with tempfile.TemporaryDirectory(prefix="ukrainian-gec-score-") as temp:
        root = Path(temp)
        source, target, hypothesis = root / "source.txt", root / "target.txt", root / "hypothesis.m2"
        source.write_text("\n".join(sources) + "\n", encoding="utf-8")
        target.write_text("\n".join(corrections) + "\n", encoding="utf-8")
        subprocess.run(["errant_parallel", "-orig", str(source), "-cor", str(target), "-out", str(hypothesis)],
                       check=True, capture_output=True, text=True)
        compared = subprocess.run(["errant_compare", "-hyp", str(hypothesis), "-ref", str(args.references)],
                                  check=True, capture_output=True, text=True)
        match = re.search(r"TP\s+FP\s+FN\s+Prec\s+Rec\s+F0\.5\s*\n([^\n]+)", compared.stdout)
        if match is None:
            raise ValueError("official metric table missing")
        values = match.group(1).split()
        if len(values) != 6:
            raise ValueError("official metric table malformed")
        counts = [int(value) for value in values[:3]]
        metrics = [float(value) for value in values[3:]]
        if any(value < 0 for value in counts) or any(not math.isfinite(value) or not 0 <= value <= 1 for value in metrics):
            raise ValueError("official metric value invalid")
        print(json.dumps({"schema": "ukrainian-llm-eval.gec-score.v1", "sentences": len(sources),
                          "tp": counts[0], "fp": counts[1], "fn": counts[2],
                          "precision": metrics[0], "recall": metrics[1], "f0_5": metrics[2],
                          "reference_sha256": hashlib.sha256(reference_bytes).hexdigest(),
                          "prediction_sha256": hashlib.sha256(corrected_bytes).hexdigest()}))


if __name__ == "__main__":
    main()
