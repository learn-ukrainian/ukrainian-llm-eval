import hashlib
import json

import pytest
from test_research_scheduling import admit, inputs, trial
from test_segmentation import _gec_source

from ukrainian_llm_eval import execution, research_scoring, scheduling
from ukrainian_llm_eval.benchmark_manifest import build_execution_plan, build_experiment_manifest
from ukrainian_llm_eval.core import ExamError, digest, prepare_exam
from ukrainian_llm_eval.evidence import EvidenceStore
from ukrainian_llm_eval.gec import prepare_gec
from ukrainian_llm_eval.research_scoring import (
    mcq_scorer_code_sha256,
    score_sealed_experiment,
    scorer_identity_sha256,
)
from ukrainian_llm_eval.segmentation import derive_segment_plan


def sealed(monkeypatch, tmp_path, *, failed=False):
    packets, plans, manifest, _, configs = inputs()
    exam = {"schema": "zno-nmt.exam.v1", "title": "synthetic", "subject": "Ukrainian", "year": 2022,
            "provenance": {"source_url": "https://example.invalid", "source_revision": "synthetic",
                           "license": "test", "exposure": "synthetic"},
            "scoring": {"kind": "benchmark", "policy_url": None, "pass_threshold": None,
                        "expected_items": 2, "expected_points": 2},
            "items": [{**item, "correct": "A"} for item in packets["ulp"]["items"]]}
    packet, key = prepare_exam(exam)
    assert packet == packets["ulp"]
    suites = manifest["suites"]
    suites[0]["key_sha256"] = digest(key)
    bindings = {"ulp": {"kind": "mcq-code", "code_sha256": mcq_scorer_code_sha256()}}
    manifest = build_experiment_manifest(manifest["protocol_sha256"], suites, manifest["routes"],
                                         tool_policy_sha256=manifest["tool_policy_sha256"],
                                         scorer_sha256=scorer_identity_sha256(bindings))
    plan = build_execution_plan(manifest)
    calls = []
    monkeypatch.setattr(execution, "run_exam", trial(calls, failed_at=1 if failed else None))
    root = tmp_path / "research"
    list(scheduling.run_research(packets, plans, manifest, plan, configs, root, admission_probe=admit))
    args = (packets, plans, {"ulp": key}, manifest, plan, configs, root)
    return args, bindings, root


def test_full_three_repeat_summary_and_paired_delta(monkeypatch, tmp_path):
    args, bindings, _ = sealed(monkeypatch, tmp_path)
    report = score_sealed_experiment(*args, scorer_bindings=bindings)
    assert len(report["cells"]) == 6 and all(cell["value"] == 2 for cell in report["cells"])
    assert len(report["summaries"]) == 2
    assert all(summary["values"] == [2, 2, 2] and summary["mean"] == 2 and summary["sample_sd"] == 0
               for summary in report["summaries"])
    assert report["paired_deltas"] == [{"suite_id": "ulp", "route_id": "fixture", "values": [0, 0, 0],
                                         "mean": 0, "sample_sd": 0}]


def test_failed_cell_keeps_explicit_no_score_and_no_partial_mean(monkeypatch, tmp_path):
    args, bindings, _ = sealed(monkeypatch, tmp_path, failed=True)
    report = score_sealed_experiment(*args, scorer_bindings=bindings)
    assert report["cells"][0]["status"] == "no_score"
    assert [summary["condition"] for summary in report["summaries"]] == ["sources"]
    assert report["paired_deltas"] == []


def test_unsealed_admission_attempt_prevents_offline_scoring(monkeypatch, tmp_path):
    args, bindings, root = sealed(monkeypatch, tmp_path)
    EvidenceStore(root / "admission-evidence").start({"denominator": 0}).finalize({"status": "failed"})
    with pytest.raises(ExamError, match="admission set differs"):
        score_sealed_experiment(*args, scorer_bindings=bindings)


def test_unsealed_request_budget_attempt_prevents_offline_scoring(monkeypatch, tmp_path):
    args, bindings, root = sealed(monkeypatch, tmp_path)
    EvidenceStore(root / "request-budget-evidence").start({"denominator": 0}).finalize({"status": "failed"})
    with pytest.raises(ExamError, match="request-budget set differs"):
        score_sealed_experiment(*args, scorer_bindings=bindings)


def test_resealed_failure_flag_cannot_drop_a_successful_cell(monkeypatch, tmp_path):
    args, bindings, root = sealed(monkeypatch, tmp_path)
    cell_path = root / (args[4]["cells"][0]["cell_id"] + ".json")
    cell = json.loads(cell_path.read_text())
    cell.update(status="failed", responses=None, failure_reason="invented_failure")
    cell_path.write_text(json.dumps(cell))
    manifest_path = root / "result-manifest.json"
    result = json.loads(manifest_path.read_text())
    result["cell_result_sha256s"][0] = digest(cell)
    result["cells_complete"] -= 1
    result["result_manifest_sha256"] = digest({key: value for key, value in result.items() if key != "result_manifest_sha256"})
    manifest_path.write_text(json.dumps(result))
    with pytest.raises(ExamError, match="cell status contradicts"):
        score_sealed_experiment(*args, scorer_bindings=bindings)


@pytest.mark.parametrize("target", ["cell", "manifest", "key", "stop"])
def test_changed_scoring_inputs_fail_closed(monkeypatch, tmp_path, target):
    args, bindings, root = sealed(monkeypatch, tmp_path)
    if target == "cell":
        path = root / (args[4]["cells"][0]["cell_id"] + ".json")
        value = json.loads(path.read_text()); value["responses"]["q0001"] = "B"
        path.write_text(json.dumps(value))
    elif target == "manifest":
        path = root / "result-manifest.json"
        value = json.loads(path.read_text()); value["receipt_set_sha256"] = "0" * 64
        path.write_text(json.dumps(value))
    elif target == "key":
        args[2]["ulp"]["answers"]["q0001"] = "B"
    else:
        (root / "stop.json").write_text('{}')
    with pytest.raises(ExamError):
        score_sealed_experiment(*args, scorer_bindings=bindings)


def test_gec_scores_full_reassembled_references_and_preserves_derived_provenance(monkeypatch, tmp_path):
    _, _, template, _, configs = inputs()
    source = _gec_source()
    packet, key = prepare_gec(source, {"source_url": "https://example.invalid", "source_revision": "synthetic",
                                       "license": "test", "exposure": "synthetic"})
    suite_id = "ua-gec-public-gec-only-test"
    denominator = {"sentences": len(packet["items"]), "documents": 2,
                   "tokens": sum(len(item["text"].split()) for item in packet["items"])}
    segmentation = derive_segment_plan(packet, suite_id=suite_id, protocol_sha256="a" * 64,
                                       denominator=denominator, gec_source=source)
    suite = {**template["suites"][0], "suite_id": suite_id, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
             "segment_plan": segmentation, "key_sha256": digest(key)}
    bindings = {suite_id: {"kind": "gec-image", "image_id": "sha256:" + "7" * 64}}
    manifest = build_experiment_manifest("a" * 64, [suite], template["routes"],
                                         tool_policy_sha256=template["tool_policy_sha256"],
                                         scorer_sha256=scorer_identity_sha256(bindings))
    plan = build_execution_plan(manifest)
    calls, scored = [], []
    ordinary = trial(calls)

    def candidate(segment, *a, **kw):
        result = ordinary(segment, *a, **kw)
        result.update(schema="ua-gec.run.v1", responses={item["id"]: item["text"] for item in segment["items"]})
        return result

    def scorer(actual_packet, actual_key, evidence_root, attempt_id, image, score_root):
        receipt = EvidenceStore(evidence_root).verify(attempt_id)
        assert actual_packet == packet and actual_key == key and image == bindings[suite_id]["image_id"]
        assert receipt["metadata"]["derived_aggregate"] is True
        assert len(receipt["metadata"]["source_receipt_sha256s"]) == 2
        assert receipt["result"]["responses"] == {item["id"]: item["text"] for item in packet["items"]}
        scored.append(attempt_id)
        return {"status": "ok", "metrics": {"f0_5": 0.5}}, {}

    monkeypatch.setattr(execution, "run_exam", candidate)
    monkeypatch.setattr(research_scoring, "score_gec_attempt", scorer)
    packets, segment_plans, keys = {suite_id: packet}, {suite_id: segmentation}, {suite_id: key}
    root, score_root = tmp_path / "research", tmp_path / "scoring"
    list(scheduling.run_research(packets, segment_plans, manifest, plan, configs, root, admission_probe=admit))
    report = score_sealed_experiment(packets, segment_plans, keys, manifest, plan, configs, root,
                                     scorer_bindings=bindings, scoring_evidence_root=score_root)
    assert len(scored) == 6 and all(cell["value"] == 0.5 for cell in report["cells"])
    with pytest.raises(ExamError, match="already scored"):
        score_sealed_experiment(packets, segment_plans, keys, manifest, plan, configs, root,
                                 scorer_bindings=bindings, scoring_evidence_root=score_root)
    assert len(scored) == 6
