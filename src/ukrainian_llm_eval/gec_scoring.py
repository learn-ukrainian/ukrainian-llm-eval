"""Offline GEC scoring bound to an authenticated execution receipt."""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from .core import ExamError, digest, read_json
from .evidence import EvidenceStore
from .gec import _contains_line_separator, validate_gec_key

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


def scoring_inputs(packet, key, run):
    """Serialize every sentence/reference in packet order without repairing answers."""
    validate_gec_key(packet, key)
    if run.get("schema") != "ua-gec.run.v1" or run.get("packet_sha256") != packet["packet_sha256"]:
        raise ExamError("GEC run is not bound to this packet")
    responses = run.get("responses")
    ids = {item["id"] for item in packet["items"]}
    if not isinstance(responses, dict) or set(responses) != ids:
        raise ExamError("GEC response denominator mismatch")
    missing = 0
    corrections, references = [], []
    for item, reference in zip(packet["items"], key["items"], strict=True):
        answer = responses[item["id"]]
        if answer is None:
            missing += 1
        elif not isinstance(answer, str) or not answer.strip() or answer.splitlines() != [answer]:
            raise ExamError("GEC correction must be one nonempty sentence line or null")
        else:
            corrections.append(answer)
        if item["text"].splitlines() != [item["text"]]:
            raise ExamError("GEC source must occupy exactly one serialized line")
        lines = ["S " + item["text"]]
        for edit in reference["annotations"]:
            if any(_contains_line_separator(edit[field]) or "|||" in edit[field]
                   for field in ("category", "replacement", "required", "metadata", "annotator_id")):
                raise ExamError("GEC annotation cannot be serialized as one M2 field")
            lines.append(f"A {edit['start']} {edit['end']}|||{edit['category']}|||{edit['replacement']}"
                         f"|||{edit['required']}|||{edit['metadata']}|||{edit['annotator_id']}")
        references.append("\n".join(lines) + "\n\n")
    return "\n".join(corrections) + "\n", "".join(references), missing


def _validated_metrics(value, count, prediction_hash, reference_hash):
    expected = {"schema", "sentences", "tp", "fp", "fn", "precision", "recall", "f0_5",
                "prediction_sha256", "reference_sha256"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ExamError("scorer output schema mismatch")
    if (value["schema"] != "ukrainian-llm-eval.gec-score.v1" or value["sentences"] != count
            or type(value["sentences"]) is not int
            or value["prediction_sha256"] != prediction_hash or value["reference_sha256"] != reference_hash):
        raise ExamError("scorer output input binding mismatch")
    if any(type(value[k]) is not int or value[k] < 0 for k in ("tp", "fp", "fn")):
        raise ExamError("scorer count invalid")
    if any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) or not 0 <= value[k] <= 1
           for k in ("precision", "recall", "f0_5")):
        raise ExamError("scorer metric invalid")
    return value


def score_gec_attempt(packet, key, run_evidence_dir, run_attempt_id, image_id, evidence_dir, *, timeout=600):
    """Score only complete responses; preserve failed scorer attempts and raw output."""
    if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
        raise ExamError("scorer image must be an immutable local sha256 image ID")
    if type(timeout) is not int or timeout <= 0:
        raise ExamError("scorer timeout must be a positive integer")
    receipt = EvidenceStore(run_evidence_dir).verify(run_attempt_id)
    if not receipt["complete"]:
        raise ExamError("execution attempt is not finalized")
    run = receipt["result"]
    predictions, references, missing = scoring_inputs(packet, key, run)
    count = len(packet["items"])
    if receipt["denominator"] != count:
        raise ExamError("execution evidence denominator differs from packet")
    prediction_hash = hashlib.sha256(predictions.encode()).hexdigest()
    reference_hash = hashlib.sha256(references.encode()).hexdigest()
    metadata = {"denominator": count, "packet_sha256": packet["packet_sha256"],
                "key_sha256": key["key_sha256"], "run_receipt_sha256": digest(receipt),
                "scorer_image": image_id, "prediction_sha256": prediction_hash,
                "reference_sha256": reference_hash,
                "wrapper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    attempt = EvidenceStore(evidence_dir).start(metadata)
    result = {"schema": "ua-gec.score.v1", **metadata, "missing_sentences": missing,
              "status": "failed", "metrics": None}
    if receipt["terminal_status"] != "completed" or run.get("status") != "ok" or missing:
        result["failure_reason"] = "incomplete_candidate_run"
        return result, attempt.finalize(result, status="failed")
    with tempfile.TemporaryDirectory(prefix="ukrainian-gec-inputs-") as temp:
        root = Path(temp)
        corrected, refs = root / "corrected.txt", root / "references.m2"
        corrected.write_text(predictions, encoding="utf-8")
        refs.write_text(references, encoding="utf-8")
        corrected.chmod(0o600)
        refs.chmod(0o600)
        attempt.append("scoring_inputs", {"predictions": predictions, "references": references})
        container_name = "ukrainian-gec-" + uuid.uuid4().hex
        command = ["docker", "run", "--rm", "--pull", "never", "--name", container_name, "--platform", "linux/amd64", "--network", "none",
                   "--read-only", "--tmpfs", "/tmp", "-v", f"{root}:/inputs:ro", image_id,
                   "--corrected", "/inputs/corrected.txt", "--references", "/inputs/references.m2"]
        try:
            completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            attempt.append("scorer_process", {"stdout_hex": (exc.stdout or b"").hex(),
                                               "stderr_hex": (exc.stderr or b"").hex(),
                                               "timeout": True})
            result["failure_reason"] = "scorer_timeout"
        except OSError:
            result["failure_reason"] = "scorer_unavailable"
        else:
            attempt.append("scorer_process", {"stdout_hex": completed.stdout.hex(),
                                               "stderr_hex": completed.stderr.hex(),
                                               "returncode": completed.returncode})
            if completed.returncode:
                result["failure_reason"] = "scorer_process_failed"
            else:
                try:
                    # Use the shared strict JSON reader: duplicate keys and nonfinite numbers fail.
                    output = root / "output.json"
                    output.write_bytes(completed.stdout)
                    output.chmod(0o600)
                    metrics = _validated_metrics(read_json(output), count, prediction_hash, reference_hash)
                except (ExamError, ValueError, UnicodeError, json.JSONDecodeError):
                    result["failure_reason"] = "invalid_scorer_output"
                else:
                    result.update(status="ok", metrics=metrics)
        finally:
            try:
                cleanup = subprocess.run(["docker", "rm", "-f", container_name], capture_output=True,
                                         timeout=30, check=False)
                attempt.append("scorer_cleanup", {"returncode": cleanup.returncode,
                                                   "stderr_hex": cleanup.stderr.hex()})
            except (OSError, subprocess.TimeoutExpired):
                attempt.append("scorer_cleanup", {"status": "unknown", "container_name": container_name})
    return result, attempt.finalize(result, status="completed" if result["status"] == "ok" else "failed")
