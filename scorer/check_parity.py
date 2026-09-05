"""Compare six synthetic cases with the hash-verified official scorer."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

OFFICIAL_URL = "https://raw.githubusercontent.com/unlp-workshop/unlp-2023-shared-task/fbff22905f8c9a3677c900d56599284151c029e6/scripts/evaluate.py"
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
        Path(temp, "official.py").write_bytes(raw)
        base = ["docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
                "--read-only", "--tmpfs", "/tmp", "--entrypoint", "/usr/local/bin/python",
                "-v", str(fixtures) + ":/fixtures:ro", "-v", temp + ":/official:ro", image_id]
        for case in sorted(fixtures.iterdir()):
            if not case.is_dir():
                continue
            corrected, references = f"/fixtures/{case.name}/corrected.txt", f"/fixtures/{case.name}/reference.m2"
            official = subprocess.run(base + ["/official/official.py", corrected, "--no-tokenize", "--m2", references],
                                      check=True, capture_output=True, text=True, timeout=120)
            wrapped = subprocess.run(base + ["/opt/scorer/score.py", "--corrected", corrected,
                                            "--no-tokenize", "--references", references],
                                     check=True, capture_output=True, text=True, timeout=120)
            match = re.search(r"TP\s+FP\s+FN\s+Prec\s+Rec\s+F0\.5\s*\n([^\n]+)", official.stdout)
            if match is None:
                raise ValueError("official metric table missing")
            expected = [float(value) for value in match.group(1).split()]
            report = json.loads(wrapped.stdout)
            actual = [report[field] for field in ("tp", "fp", "fn", "precision", "recall", "f0_5")]
            if actual != expected:
                raise ValueError("scorer parity failed for " + case.name)
            reports[case.name] = {"metrics": actual, "parity": True}
            print(case.name + ": parity passed", flush=True)
    if len(reports) != 6:
        raise ValueError("parity case denominator mismatch")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        json.dump({"image_id": image_id, "official_sha256": OFFICIAL_SHA256, "cases": reports}, output, indent=2)


if __name__ == "__main__":
    main()
