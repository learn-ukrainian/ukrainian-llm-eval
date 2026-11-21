"""Compare six synthetic cases with the hash-verified official scorer."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

OFFICIAL_URL = "https://raw.githubusercontent.com/unlp-workshop/unlp-2023-shared-task/fbff22905f8c9a3677c900d56599284151c029e6/scripts/evaluate.py"
EXPECTED_COUNTS = {
    "deletion": [1, 0, 0], "insertion": [1, 0, 0], "replacement": [1, 0, 0],
    "two_references": [1, 0, 0], "unchanged": [0, 0, 1], "wrong": [0, 1, 1],
}

OFFICIAL_SHA256 = "6e37b7a41a3a3c303647ca29507cd51b4b6deb9b0952c2a62d8e0b0374fae31a"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="ukrainian-llm-eval-scorer:locked")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("refusing to replace parity evidence")
    fixtures = Path(__file__).resolve().parent / "fixtures"
    raw = urllib.request.urlopen(OFFICIAL_URL, timeout=30).read()
    if hashlib.sha256(raw).hexdigest() != OFFICIAL_SHA256:
        raise ValueError("official script hash mismatch")
    image_id = subprocess.check_output(["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True).strip()
    reports = {}
    with tempfile.TemporaryDirectory(prefix="gec-parity-") as temp:
        official_script = Path(temp, "official.py")
        official_script.write_bytes(raw)
        official_script.chmod(0o444)
        base = ["docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
                "--read-only", "--tmpfs", "/tmp", "--entrypoint", "/usr/local/bin/python",
                "-v", f"{official_script}:/official.py:ro"]
        for case in sorted(fixtures.iterdir()):
            if not case.is_dir():
                continue
            case_inputs = Path(temp, case.name)
            case_inputs.mkdir()
            case_inputs.chmod(0o755)
            for name in ("corrected.txt", "reference.m2"):
                staged_file = case_inputs / name
                shutil.copyfile(case / name, staged_file)
                staged_file.chmod(0o444)
            run = base + ["-v", f"{case_inputs}:/fixtures:ro", image_id]
            corrected, references = "/fixtures/corrected.txt", "/fixtures/reference.m2"
            official = subprocess.run(run + ["/official.py", corrected, "--no-tokenize", "--m2", references],
                                      check=True, capture_output=True, text=True, timeout=120)
            scorer = run + ["/opt/scorer/score.py", "--corrected", corrected, "--references", references]
            wrapped = subprocess.run(scorer + ["--no-tokenize"],
                                     check=True, capture_output=True, text=True, timeout=120)
            match = re.search(r"TP\s+FP\s+FN\s+Prec\s+Rec\s+F0\.5\s*\n([^\n]+)", official.stdout)
            if match is None:
                raise ValueError("official metric table missing")
            expected = [float(value) for value in match.group(1).split()]
            report = json.loads(wrapped.stdout)
            actual = [report[field] for field in ("tp", "fp", "fn", "precision", "recall", "f0_5")]
            if actual[:3] != EXPECTED_COUNTS[case.name]:
                raise ValueError("fixture semantic outcome failed for " + case.name)
            if actual != expected:
                raise ValueError("scorer parity failed for " + case.name)
            if case.name == "replacement":
                tokenized = subprocess.run(scorer, check=True, capture_output=True, text=True, timeout=120)
                if json.loads(tokenized.stdout) != report:
                    raise ValueError("default tokenization changed the replacement result")
                print("replacement: default tokenization passed", flush=True)
            reports[case.name] = {"metrics": actual, "parity": True}
            print(case.name + ": parity passed", flush=True)
    if len(reports) != 6:
        raise ValueError("parity case denominator mismatch")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        json.dump({"image_id": image_id, "official_sha256": OFFICIAL_SHA256, "cases": reports}, output, indent=2)


if __name__ == "__main__":
    main()
