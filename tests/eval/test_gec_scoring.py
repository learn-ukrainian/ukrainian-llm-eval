import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from ukrainian_llm_eval import gec_scoring
from ukrainian_llm_eval.core import ExamError
from ukrainian_llm_eval.evidence import EvidenceIntegrityError, EvidenceStore
from ukrainian_llm_eval.gec import prepare_gec

IMAGE = "sha256:" + "a" * 64


def inputs(tmp_path, answer="Я люблю мову ."):
    packet, key = prepare_gec(
        "S Я дуже люблю мову .\n"
        "A 1 2|||G/Other||||||REQUIRED|||-NONE-|||0\n"
        "A 1 2|||G/Other||||||REQUIRED|||-NONE-|||1\n\n",
        {"source_url": "https://example.org", "source_revision": "synthetic", "license": "MIT", "exposure": "synthetic"})
    run = {"schema": "ua-gec.run.v1", "packet_sha256": packet["packet_sha256"],
           "status": "ok", "responses": {"q0001": answer}}
    store = EvidenceStore(tmp_path / "runs")
    store.start({"denominator": 1}, attempt_id="candidate").finalize(run)
    return packet, key


def score(tmp_path, packet, key):
    return gec_scoring.score_gec_attempt(packet, key, tmp_path / "runs", "candidate", IMAGE, tmp_path / "scores")


def fake_scorer(command, **kwargs):
    if command[1] == "rm":
        return subprocess.CompletedProcess(command, 0, b"", b"")
    assert "none" == command[command.index("--network") + 1]
    assert "never" == command[command.index("--pull") + 1]
    mount = command[command.index("-v") + 1]
    assert mount.endswith(":/inputs:ro")
    root = Path(mount.removesuffix(":/inputs:ro"))
    assert "|||0" in (root / "references.m2").read_text()
    assert "|||1" in (root / "references.m2").read_text()
    result = {"schema": "ukrainian-llm-eval.gec-score.v1", "sentences": 1,
              "tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f0_5": 1.0,
              "prediction_sha256": hashlib.sha256((root / "corrected.txt").read_bytes()).hexdigest(),
              "reference_sha256": hashlib.sha256((root / "references.m2").read_bytes()).hexdigest()}
    return subprocess.CompletedProcess(command, 0, json.dumps(result).encode(), b"diagnostic")


def test_score_binds_all_inputs_and_retains_raw_output(tmp_path, monkeypatch):
    packet, key = inputs(tmp_path)
    monkeypatch.setattr(gec_scoring.subprocess, "run", fake_scorer)
    result, receipt = score(tmp_path, packet, key)
    assert result["status"] == "ok" and result["metrics"]["tp"] == 1
    assert result["key_sha256"] == key["key_sha256"]
    assert receipt["complete"] and receipt["terminal_status"] == "completed"
    log = next((tmp_path / "scores" / "attempts").glob("*/events.jsonl")).read_text()
    assert b"diagnostic".hex() in log and "scoring_inputs" in log


def test_missing_answer_never_invokes_scorer_or_shrinks_denominator(tmp_path, monkeypatch):
    packet, key = inputs(tmp_path, None)
    monkeypatch.setattr(gec_scoring.subprocess, "run", lambda *a, **kw: pytest.fail("scorer called"))
    result, receipt = score(tmp_path, packet, key)
    assert result["missing_sentences"] == result["denominator"] == 1
    assert result["metrics"] is None and receipt["terminal_status"] == "failed"


def test_corrupt_execution_rejected_before_scoring(tmp_path, monkeypatch):
    packet, key = inputs(tmp_path)
    log = tmp_path / "runs/attempts/candidate/events.jsonl"
    log.write_bytes(log.read_bytes()[:-1])
    monkeypatch.setattr(gec_scoring.subprocess, "run", lambda *a, **kw: pytest.fail("scorer called"))
    with pytest.raises(EvidenceIntegrityError):
        score(tmp_path, packet, key)
    assert not (tmp_path / "scores").exists()


def test_scorer_wrong_input_hash_fails_with_evidence(tmp_path, monkeypatch):
    packet, key = inputs(tmp_path)

    def changed(command, **kwargs):
        completed = fake_scorer(command, **kwargs)
        if command[1] == "run":
            report = json.loads(completed.stdout)
            report["prediction_sha256"] = "b" * 64
            completed.stdout = json.dumps(report).encode()
        return completed

    monkeypatch.setattr(gec_scoring.subprocess, "run", changed)
    result, receipt = score(tmp_path, packet, key)
    assert result["failure_reason"] == "invalid_scorer_output"
    assert receipt["terminal_status"] == "failed"


def test_timeout_preserves_partial_bytes_and_reaps_owned_container(tmp_path, monkeypatch):
    packet, key = inputs(tmp_path)
    calls = []

    def timeout(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 1, output=b"partial\xff", stderr=b"error")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(gec_scoring.subprocess, "run", timeout)
    result, receipt = score(tmp_path, packet, key)
    assert result["failure_reason"] == "scorer_timeout"
    assert calls[1][-1] == calls[0][calls[0].index("--name") + 1]
    assert receipt["terminal_status"] == "failed"
    log = next((tmp_path / "scores" / "attempts").glob("*/events.jsonl")).read_text()
    assert b"partial\xff".hex() in log


def test_mutable_image_tag_rejected(tmp_path):
    packet, key = inputs(tmp_path)
    with pytest.raises(ExamError, match="immutable"):
        gec_scoring.score_gec_attempt(packet, key, tmp_path / "runs", "candidate", "latest", tmp_path / "scores")


def test_cli_missing_run_preserves_failed_score_and_numeric_denominator(tmp_path):
    import sys

    packet, key = inputs(tmp_path, None)
    questions, references = tmp_path / "questions.json", tmp_path / "key.json"
    questions.write_text(json.dumps(packet))
    references.write_text(json.dumps(key))
    output = tmp_path / "score.json"
    command = [sys.executable, "-m", "ukrainian_llm_eval", "score-gec", "--questions", str(questions),
               "--key", str(references), "--run-evidence-dir", str(tmp_path / "runs"),
               "--attempt-id", "candidate", "--scorer-image", IMAGE,
               "--evidence-dir", str(tmp_path / "scores"), "--output", str(output)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 2, completed.stderr
    public = json.loads(completed.stdout)
    assert public["denominator"] == public["missing_sentences"] == 1
    assert "references" not in completed.stdout and "Я" not in completed.stdout
    report = json.loads(output.read_text())
    assert report["failure_reason"] == "incomplete_candidate_run"
    receipt = json.loads(output.with_suffix(".evidence.json").read_text())
    assert receipt["terminal_status"] == "failed"
    assert output.stat().st_mode & 0o777 == 0o600
    before = output.read_bytes()
    again = subprocess.run(command, capture_output=True, text=True, check=False)
    assert again.returncode == 2
    assert output.read_bytes() == before


@pytest.mark.parametrize("answer", ["речення\n", "речення\r", "речення\u2028", {"text": "речення"}])
def test_invalid_sentence_shape_rejected_without_scoring(tmp_path, monkeypatch, answer):
    packet, key = inputs(tmp_path, answer)
    monkeypatch.setattr(gec_scoring.subprocess, "run", lambda *a, **kw: pytest.fail("scorer called"))
    with pytest.raises(ExamError, match="one nonempty sentence"):
        score(tmp_path, packet, key)
    assert not (tmp_path / "scores").exists()
