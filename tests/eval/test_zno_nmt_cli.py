"""Source-blind public CLI behavior using synthetic examples only."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ukrainian_llm_eval.__main__ import public_aggregate
from ukrainian_llm_eval.core import ExamError
from ukrainian_llm_eval.gec import prepare_gec
from ukrainian_llm_eval.importers import import_zno


def metadata():
    return {
        "title": "Synthetic", "subject": "synthetic", "year": 2026,
        "provenance": {"source_url": "https://example.org/test", "source_revision": "v1",
                       "license": "synthetic", "exposure": "public-exposure-possible"},
        "scoring": {"kind": "benchmark", "policy_url": None, "pass_threshold": None,
                    "expected_items": 1, "expected_points": 1},
    }


def source():
    return [{"test_id": "1", "num_tasks": 1, "tasks": [{
        "task_id": 0, "question": "Choose X.", "answers": [
            {"answer": "A", "text": "X"}, {"answer": "B", "text": "Y"}],
        "answer_vheader": ["A", "B"], "answer_hheader": [], "correct_answer": ["A"],
        "comment": "SECRET EXPLANATION", "with_photo": False,
    }]}]


def test_import_does_not_copy_explanations():
    exam = import_zno(source(), "1", metadata())
    assert "SECRET EXPLANATION" not in json.dumps(exam)
    assert exam["items"][0]["id"] == "1"


def test_complete_import_rejects_images_and_count_drift():
    paper = source()
    paper[0]["tasks"][0]["with_photo"] = True
    with pytest.raises(ExamError, match="image"):
        import_zno(paper, "1", metadata())
    paper = source()
    paper[0]["num_tasks"] = 2
    with pytest.raises(ExamError, match="count"):
        import_zno(paper, "1", metadata())


def test_export_is_allowlisted_and_cannot_copy_text():
    report = {"raw_points": 1, "max_points": 2, "passed": None,
              "items": [{"answer": "private"}], "identity": {"secret": "private"},
              "metrics": {"elapsed_seconds": 2.0, "endpoint": "private"},
              "denominator": {"expected_items": 2, "prompt": "private"},
              "raw_points_delta": "private", "tool_calls": ["private"]}
    result = public_aggregate(report)
    assert "private" not in json.dumps(result)
    assert result["raw_points"] == 1
    assert result["metrics"] == {"elapsed_seconds": 2.0}
    assert "raw_points_delta" not in result


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "ukrainian_llm_eval", *map(str, args)],
                          capture_output=True, text=True, timeout=20, check=False,
                          cwd=Path(__file__).resolve().parents[2])


def test_cli_separates_key_and_refuses_overwrite(tmp_path):
    exam = tmp_path / "exam.json"
    exam.write_text(json.dumps(import_zno(source(), "1", metadata())))
    packet, key = tmp_path / "questions.json", tmp_path / "key.json"
    result = run_cli("prepare", "--exam", exam, "--questions", packet, "--key", key)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(packet.read_text())
    assert "correct" not in json.dumps(data)
    assert "SECRET" not in packet.read_text()
    assert packet.stat().st_mode & 0o777 == 0o600
    assert key.stat().st_mode & 0o777 == 0o600
    assert run_cli("prepare", "--exam", exam, "--questions", packet, "--key", key).returncode == 2


def test_runner_cli_cannot_accept_key():
    result = run_cli("run", "--key", "private")
    assert result.returncode == 2


def test_evidence_status_reports_intact_attempt_alongside_corruption(tmp_path):
    from ukrainian_llm_eval.evidence import EvidenceStore

    root = tmp_path / "evidence"
    store = EvidenceStore(root)
    store.start({"denominator": 1}, attempt_id="intact").finalize({"status": "ok"})
    store.start({"denominator": 1}, attempt_id="damaged")
    log = root / "attempts" / "damaged" / "events.jsonl"
    log.write_bytes(log.read_bytes()[:-1])
    original = log.read_bytes()
    output = tmp_path / "inspection.json"
    result = run_cli("evidence-status", "--evidence-dir", root, "--output", output)
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout) == {"attempts": 2, "complete": 1, "corrupt": 1}
    report = json.loads(output.read_text())
    assert report["intact"]["complete"] is True
    assert report["damaged"]["status"] == "corrupt"
    assert log.read_bytes() == original
    assert output.stat().st_mode & 0o777 == 0o600


def test_ulp_cli_binds_source_and_keeps_category_outside_packet(tmp_path):
    import hashlib

    raw = tmp_path / "rows.jsonl"
    raw.write_text(json.dumps({"question": "Оберіть слово.", "choices": ["один", "два", "три"],
                               "answer": 0, "answer_letter": "А", "debug_id": "source-private-id",
                               "category": "grammar"}, ensure_ascii=False) + "\n")
    meta = tmp_path / "metadata.json"
    meta.write_text(json.dumps(metadata()))
    exam, sidecar = tmp_path / "exam.json", tmp_path / "sidecar.json"
    args = ("import-ulp", "--input", raw, "--metadata", meta, "--output", exam, "--sidecar", sidecar)
    assert run_cli(*args, "--source-sha256", "wrong").returncode == 2
    assert not exam.exists() and not sidecar.exists()
    source_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    result = run_cli(*args, "--source-sha256", source_hash)
    assert result.returncode == 0, result.stdout + result.stderr
    packet, key = tmp_path / "packet.json", tmp_path / "key.json"
    assert run_cli("prepare", "--exam", exam, "--questions", packet, "--key", key).returncode == 0
    receipt = json.loads(sidecar.read_text())
    assert receipt["source_sha256"] == source_hash
    assert receipt["packet_sha256"] == json.loads(packet.read_text())["packet_sha256"]
    assert "source-private-id" not in packet.read_text()
    assert "grammar" not in packet.read_text()
    assert "correct" not in packet.read_text()
    assert run_cli(*args).returncode == 2


def test_gec_cli_keeps_both_references_private(tmp_path):
    source = tmp_path / "input.m2"
    source.write_text("S Я люблю мову .\n"
                      "A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||0\n"
                      "A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n\n")
    provenance = tmp_path / "provenance.json"
    provenance.write_text(json.dumps({"source_url": "https://example.org/synthetic",
                                      "source_revision": "fixture", "license": "MIT", "exposure": "synthetic"}))
    packet, key = tmp_path / "packet.json", tmp_path / "key.json"
    args = ("prepare-gec", "--input", source, "--provenance", provenance,
            "--questions", packet, "--key", key)
    assert run_cli(*args, "--source-sha256", "wrong").returncode == 2
    assert not packet.exists() and not key.exists()
    result = run_cli(*args, "--source-sha256", hashlib.sha256(source.read_bytes()).hexdigest())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["prepared_sentences"] == 1
    assert "annotations" not in packet.read_text()
    refs = json.loads(key.read_text())["items"][0]["annotations"]
    assert {ref["annotator_id"] for ref in refs} == {"0", "1"}
    assert packet.stat().st_mode & 0o777 == key.stat().st_mode & 0o777 == 0o600
    assert run_cli(*args).returncode == 2


def _gec_verification_fixture(tmp_path, *, denominator=None):
    raw = (
        "S Це тест .\n"
        "A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||0\n"
        "A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n\n"
    ).encode()
    provenance = {
        "source_url": "https://example.org/synthetic-gec",
        "source_revision": "synthetic-cli-r1",
        "license": "synthetic",
        "exposure": "synthetic",
    }
    packet, key = prepare_gec(raw.decode("utf-8"), provenance)
    profile = {
        "id": "ua-gec-public-gec-only-test",
        "revision": provenance["source_revision"],
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "license": provenance["license"],
        "denominator": denominator if denominator is not None else {"sentences": 1},
    }
    profiles = {"schema": "ukrainian-llm-eval.benchmark-sources.v1", "suites": [profile]}
    paths = {
        "profiles": tmp_path / "profiles.json",
        "source": tmp_path / "source.m2",
        "questions": tmp_path / "questions.json",
        "key": tmp_path / "key.json",
        "output": tmp_path / "manifest.json",
    }
    paths["profiles"].write_text(json.dumps(profiles, ensure_ascii=False))
    paths["source"].write_bytes(raw)
    paths["questions"].write_text(json.dumps(packet, ensure_ascii=False))
    paths["key"].write_text(json.dumps(key, ensure_ascii=False))
    args = (
        "verify-benchmark",
        "--profiles", paths["profiles"],
        "--suite", profile["id"],
        "--source", paths["source"],
        "--questions", paths["questions"],
        "--key", paths["key"],
        "--output", paths["output"],
    )
    return paths, args, profile


def test_verify_benchmark_cli_succeeds_fresh_and_preserves_existing_manifest(tmp_path):
    paths, args, profile = _gec_verification_fixture(tmp_path)

    fresh = run_cli(*args)
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    report_bytes = paths["output"].read_bytes()
    report = json.loads(report_bytes)
    assert report["verification"] == "matches_supplied_profile"
    assert report["denominator"] == profile["denominator"]
    assert json.loads(fresh.stdout)["packet_sha256"] == report["packet_sha256"]

    repeated = run_cli(*args)
    assert repeated.returncode == 2
    assert paths["output"].read_bytes() == report_bytes


def test_verify_benchmark_cli_rejects_malformed_profile_without_output(tmp_path):
    paths, args, _profile = _gec_verification_fixture(tmp_path)
    malformed = json.loads(paths["profiles"].read_text())
    malformed["suites"][0]["denominator"] = {"sentences": 1, "unknown": 3}
    paths["profiles"].write_text(json.dumps(malformed, ensure_ascii=False))

    result = run_cli(*args)

    assert result.returncode == 2
    assert not paths["output"].exists()


def test_typography_cli_restores_emphasis_without_revealing_key(tmp_path):
    from ukrainian_llm_eval.core import digest

    original = import_zno(source(), "1", metadata())
    exam, overlay = tmp_path / "exam.json", tmp_path / "overlay.json"
    exam.write_text(json.dumps(original))
    overlay.write_text(json.dumps({"schema": "ukrainian-llm-eval.typography.v1",
                                   "source_exam_sha256": digest(original),
                                   "official_source": {"url": "https://example.org/paper.pdf", "sha256": "a" * 64},
                                   "changes": [{"item_id": "1", "field": "question", "original_text": "Choose",
                                                "replacement_text": "*Choose*"}]}))
    output, receipt = tmp_path / "restored.json", tmp_path / "receipt.json"
    args = ("apply-typography", "--exam", exam, "--overlay", overlay, "--output", output, "--receipt", receipt)
    result = run_cli(*args)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(output.read_text())["items"][0]["question"] == "*Choose* X."
    assert json.loads(receipt.read_text())["change_count"] == 1
    assert "Choose" not in receipt.read_text() and "correct" not in receipt.read_text()
    assert output.stat().st_mode & 0o777 == receipt.stat().st_mode & 0o777 == 0o600
    assert run_cli(*args).returncode == 2
