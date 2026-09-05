import json
import os
import subprocess
import sys

from test_research_scheduling import inputs
from test_research_scoring import sealed


def invoke(*args, cwd):
    return subprocess.run([sys.executable, "-m", "ukrainian_llm_eval", *map(str, args)],
                          cwd=cwd, env=os.environ.copy(), text=True, capture_output=True, check=False)


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_cli_preparation_creates_gold_free_plan_and_preserves_existing_output(tmp_path):
    packets, plans, *_ = inputs()
    questions = write(tmp_path / "questions.json", packets["ulp"])
    denominator = write(tmp_path / "denominator.json", {"items": 2, "points": 2})
    output = tmp_path / "segments.json"
    argv = ("prepare-segments", "--questions", questions, "--denominator", denominator,
            "--suite", "ulp", "--protocol-sha256", "a" * 64, "--output", output)
    result = invoke(*argv, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(output.read_text()) == plans["ulp"]
    before = output.read_bytes()
    assert "Виберіть" not in output.read_text() and "correct" not in output.read_text()
    assert invoke(*argv, cwd=tmp_path).returncode == 2
    assert output.read_bytes() == before


def test_cli_research_plan_checks_entire_cap_before_outputs_and_does_not_admit_execution(tmp_path):
    _, _, manifest, plan, _ = inputs(metered=True)
    spec = {name: manifest[name] for name in ("protocol_sha256", "suites", "routes", "scorer_sha256",
                                             "tool_policy_sha256", "repeats", "new_spend_cap_micro_usd")}
    source = write(tmp_path / "specification.json", spec)
    out_manifest, out_plan = tmp_path / "manifest.json", tmp_path / "plan.json"
    argv = ("plan-research", "--specification", source, "--manifest", out_manifest, "--execution-plan", out_plan)
    result = invoke(*argv, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["execution_admitted"] is False
    assert json.loads(out_manifest.read_text()) == manifest
    assert json.loads(out_plan.read_text()) == plan
    assert not (tmp_path / "evidence").exists()
    spec["new_spend_cap_micro_usd"] = plan["reservation_total_micro_usd"] - 1
    write(source, spec)
    rejected_manifest, rejected_plan = tmp_path / "rejected-manifest.json", tmp_path / "rejected-plan.json"
    result = invoke("plan-research", "--specification", source, "--manifest", rejected_manifest,
                    "--execution-plan", rejected_plan, cwd=tmp_path)
    assert result.returncode == 2
    assert not rejected_manifest.exists() and not rejected_plan.exists()


def test_cli_offline_research_scoring_uses_relative_custodian_paths(monkeypatch, tmp_path):
    args, bindings, execution_root = sealed(monkeypatch, tmp_path)
    packets, segments, keys, manifest, plan, configs, _ = args
    maps = {"schema": "ukrainian-llm-eval.research-scoring-inputs.v1"}
    for field, values in (("packets", packets), ("segment_plans", segments), ("keys", keys), ("configs", configs)):
        maps[field] = {}
        for identifier, value in values.items():
            path = write(tmp_path / (field + "-" + identifier + ".json"), value)
            maps[field][identifier] = path.name
    input_path = write(tmp_path / "scoring-inputs.json", maps)
    manifest_path = write(tmp_path / "manifest.json", manifest)
    plan_path = write(tmp_path / "plan.json", plan)
    bindings_path = write(tmp_path / "bindings.json", bindings)
    output = tmp_path / "report.json"
    argv = ("score-research", "--inputs", input_path, "--manifest", manifest_path, "--execution-plan", plan_path,
            "--execution-root", execution_root, "--scorer-bindings", bindings_path, "--output", output)
    result = invoke(*argv, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"cells": 6, "cells_scored": 6, "complete_triples": 2, "complete_pairs": 1}
    assert len(json.loads(output.read_text())["cells"]) == 6
    before = output.read_bytes()
    assert invoke(*argv, cwd=tmp_path).returncode == 2
    assert output.read_bytes() == before
