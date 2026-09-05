"""Task failures remain failures without selecting only successful repeats."""

import json

import pytest
from test_research_scheduling import admit, inputs

from ukrainian_llm_eval import execution, scheduling
from ukrainian_llm_eval.benchmark_manifest import build_execution_plan, build_experiment_manifest
from ukrainian_llm_eval.core import digest
from ukrainian_llm_eval.evidence import EvidenceStore


def _inputs():
    packets, plans, original, _, configs = inputs()
    config = {**configs["fixture"], "adapter": "kimi", "model": "kimi-code/k3", "provider": "managed:kimi-code"}
    route = {**original["routes"][0], "config_sha256": digest(config),
             "route_sha256": execution.route_fingerprint(config, None)}
    manifest = build_experiment_manifest(original["protocol_sha256"], original["suites"], [route],
                                         scorer_sha256=original["scorer_sha256"],
                                         tool_policy_sha256=scheduling.research_implementation_sha256())
    return packets, plans, manifest, build_execution_plan(manifest), {"fixture": config}


def _failed(packet, config, session):
    identity = {"adapter": "kimi", "harness": "kimi-cli", "provider": "managed:kimi-code",
                "model": config["model"], "requested_model": config["model"],
                "requested_model_alias": config["model"], "effective_model": "unknown",
                "effective_effort": "unknown", "cli_version": "fixture", "session_id": session}
    identity.update({field: "a" * 64 for field in
                     ("binary_sha256", "native_config_sha256", "catalog_provider_sha256", "catalog_model_sha256")})
    return {"schema": "zno-nmt.run.v1", "status": "failed", "failure_reason": "candidate_response_error",
            "packet_sha256": packet["packet_sha256"], "identity": identity, "metrics": {"tool_calls": 0},
            "responses": {item["id"]: None for item in packet["items"]}}


def test_pair_preserves_task_failures_and_executes_all_independent_repeats(monkeypatch, tmp_path):
    packets, _, _, _, configs = _inputs()
    calls = []
    monkeypatch.setattr(scheduling, "preflight", lambda *a: {})

    def execute(packet, config, condition, **kwargs):
        calls.append(condition)
        return _failed(packet, config, f"session-{len(calls)}")

    monkeypatch.setattr(execution, "run_exam", execute)
    root = tmp_path / "pair"
    progress = list(scheduling.run_pair(packets["ulp"], configs["fixture"], root))
    assert len(progress) == len(calls) == 6
    assert all(item["status"] == "failed" for item in progress)
    receipts = EvidenceStore(root / "evidence").verify_all()
    assert len(receipts) == 6
    assert all(r["result"]["responses"] == {"q0001": None, "q0002": None} for r in receipts.values())
    list(scheduling.run_pair(packets["ulp"], configs["fixture"], root, resume=True))
    assert len(calls) == 6


@pytest.mark.parametrize("reuse", [False, True])
def test_research_task_failure_retains_cell_policy_and_checks_session_reuse(monkeypatch, tmp_path, reuse):
    args = _inputs()
    calls = []

    def execute(packet, config, condition, **kwargs):
        calls.append(condition)
        return _failed(packet, config, "reused" if reuse else f"session-{len(calls)}")

    monkeypatch.setattr(execution, "run_exam", execute)
    root = tmp_path / "research"
    progress = list(scheduling.run_research(*args, root, admission_probe=admit))
    if reuse:
        assert len(calls) == 2
        assert progress[-1] == {"status": "stopped", "reason": "missing_or_reused_session"}
    else:
        assert len(calls) == len(progress) == 6
        assert all(item["status"] == "failed" for item in progress)
        manifest = json.loads((root / "result-manifest.json").read_text())
        assert manifest["cells_required"] == manifest["attempts_started"] == 6
        assert manifest["cells_complete"] == 0
    assert len(EvidenceStore(root / "evidence").verify_all()) == len(calls)


def test_task_failure_cannot_hide_observed_token_overrun():
    packets, _, manifest, _, configs = _inputs()
    result = _failed(packets["ulp"], configs["fixture"], "session")
    result["metrics"]["output_tokens"] = 301
    assert scheduling._research_stop_reason(result, manifest["routes"][0], configs["fixture"]) == "token_reservation_overrun"
