import json

import pytest
from test_research_scheduling import inputs

from ukrainian_llm_eval import adapters, native_kimi, runner
from ukrainian_llm_eval.candidate_outcome import CANDIDATE_RESPONSE_ERROR


@pytest.mark.parametrize("drift", [False, True])
def test_runner_dispatches_kimi_with_private_runtime_path_and_checks_drift(monkeypatch, tmp_path, drift):
    config = {
        "schema": "zno-nmt.config.v1", "adapter": "kimi", "model": "kimi-code/k3", "effort": "high",
        "timeout_seconds": 20, "max_output_tokens": 100, "max_tool_calls": 1, "repeats": 1,
        "tools": [], "corpus_id": None, "provider": "managed:kimi-code",
    }
    private = str(tmp_path / "operator-provisioning")
    monkeypatch.setenv("UKRAINIAN_LLM_EVAL_KIMI_PROVISIONING_DIR", private)
    fingerprints = {field: "a" * 64 for field in (
        "binary_sha256", "native_config_sha256", "catalog_provider_sha256", "catalog_model_sha256")}
    seen = []

    def preflight(checked, condition, sources_url, *, private_env_path):
        seen.append(private_env_path)
        assert condition == "closed-book" and sources_url is None
        return {**fingerprints, "tool_schema_sha256": adapters.digest([]), "mcp_server_identity_sha256": None}

    def run(packet, checked, condition, *, private_env_path, **kwargs):
        seen.append(private_env_path)
        return {
            "responses": {item["id"]: "A" for item in packet["items"]},
            "identity": {**fingerprints, "session_id": "native-session", "effective_model": "unknown",
                         "binary_sha256": "b" * 64 if drift else fingerprints["binary_sha256"]},
            "metrics": runner._empty_metrics(),
        }

    monkeypatch.setattr(native_kimi, "preflight", preflight)
    monkeypatch.setattr(native_kimi, "run_kimi", run)
    result = runner.run_exam(inputs()[0]["ulp"], config, "closed-book")
    assert seen == [private, private]
    assert result["status"] == ("failed" if drift else "ok")
    assert private not in json.dumps(result)
    assert private not in json.dumps(adapters.validate_config(config))


def test_runner_preserves_verified_kimi_task_failure_identity_and_metrics(monkeypatch):
    config = {
        "schema": "zno-nmt.config.v1", "adapter": "kimi", "model": "kimi-code/k3", "effort": "high",
        "timeout_seconds": 20, "max_output_tokens": 100, "max_tool_calls": 1, "repeats": 1,
        "tools": [], "corpus_id": None, "provider": "managed:kimi-code",
    }
    fingerprints = {field: "a" * 64 for field in (
        "binary_sha256", "native_config_sha256", "catalog_provider_sha256", "catalog_model_sha256")}
    monkeypatch.setenv("UKRAINIAN_LLM_EVAL_KIMI_PROVISIONING_DIR", "/private/operator-path")

    def preflight(checked, condition, sources_url, *, private_env_path):
        assert checked["adapter"] == "kimi"
        assert condition == "closed-book" and sources_url is None
        return {**fingerprints, "tool_schema_sha256": adapters.digest([]), "mcp_server_identity_sha256": None}

    def run(packet, checked, condition, *, private_env_path, **kwargs):
        return {
            "status": "failed",
            "failure_reason": CANDIDATE_RESPONSE_ERROR,
            "responses": {item["id"]: None for item in packet["items"]},
            "identity": {
                **fingerprints,
                "adapter": "kimi",
                "harness": "kimi-cli",
                "provider": "managed:kimi-code",
                "model": "kimi-code/k3",
                "requested_model": "kimi-code/k3",
                "requested_model_alias": "kimi-code/k3",
                "cli_version": "0.41.0",
                "session_id": "verified-session",
                "effective_model": "unknown",
            },
            "metrics": {**runner._empty_metrics(), "tool_calls": 1, "total_tokens": 7},
        }

    monkeypatch.setattr(native_kimi, "preflight", preflight)
    monkeypatch.setattr(native_kimi, "run_kimi", run)
    result = runner.run_exam(inputs()[0]["ulp"], config, "closed-book")

    assert result["status"] == "failed"
    assert result["failure_reason"] == CANDIDATE_RESPONSE_ERROR
    assert result["identity"]["session_id"] == "verified-session"
    assert result["metrics"]["tool_calls"] == 1
    assert result["metrics"]["total_tokens"] == 7
    assert result["responses"] == {"q0001": None, "q0002": None}


def test_runner_fail_closed_when_candidate_failure_lacks_verified_envelope(monkeypatch):
    config = {
        "schema": "zno-nmt.config.v1", "adapter": "kimi", "model": "kimi-code/k3", "effort": "high",
        "timeout_seconds": 20, "max_output_tokens": 100, "max_tool_calls": 1, "repeats": 1,
        "tools": [], "corpus_id": None, "provider": "managed:kimi-code",
    }
    fingerprints = {field: "a" * 64 for field in (
        "binary_sha256", "native_config_sha256", "catalog_provider_sha256", "catalog_model_sha256")}
    monkeypatch.setattr(
        native_kimi,
        "preflight",
        lambda *_args, **_kwargs: {
            **fingerprints, "tool_schema_sha256": adapters.digest([]), "mcp_server_identity_sha256": None,
        },
    )
    monkeypatch.setattr(
        native_kimi,
        "run_kimi",
        lambda *_args, **_kwargs: {
            "status": "failed",
            "failure_reason": CANDIDATE_RESPONSE_ERROR,
            "responses": {"q0001": None, "q0002": None},
            "identity": {"session_id": None},
            "metrics": {"tool_calls": 1},
        },
    )

    result = runner.run_exam(inputs()[0]["ulp"], config, "closed-book")

    assert result["status"] == "failed"
    assert result["failure_reason"] == "configuration_error"
    assert result["identity"]["session_id"] is None
    assert result["metrics"] == runner._empty_metrics()
