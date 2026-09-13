import json

import pytest
from test_research_scheduling import inputs

from ukrainian_llm_eval import adapters, native_cursor, runner
from ukrainian_llm_eval.candidate_outcome import CANDIDATE_RESPONSE_ERROR


@pytest.mark.parametrize("drift", [False, True])
def test_runner_dispatches_cursor_and_checks_drift(monkeypatch, drift):
    config = {
        "schema": "zno-nmt.config.v1",
        "adapter": "cursor",
        "model": "cursor-grok-4.6-high",
        "effort": "high",
        "timeout_seconds": 20,
        "max_output_tokens": 100,
        "max_tool_calls": 1,
        "repeats": 1,
        "tools": [],
        "corpus_id": None,
        "provider": "managed:cursor-subscription",
    }
    fingerprints = {
        field: "a" * 64
        for field in (
            "binary_sha256",
            "native_config_sha256",
            "catalog_provider_sha256",
            "catalog_model_sha256",
            "settings_sha256",
            "request_shape_sha256",
        )
    }

    def preflight(checked, condition, sources_url):
        assert condition == "closed-book" and sources_url is None
        return {**fingerprints, "tool_schema_sha256": adapters.digest([]), "mcp_server_identity_sha256": None}

    def run(packet, checked, condition, **kwargs):
        return {
            "responses": {item["id"]: "A" for item in packet["items"]},
            "identity": {
                **fingerprints,
                "session_id": "native-session",
                "effective_model": "unknown",
                "binary_sha256": "b" * 64 if drift else fingerprints["binary_sha256"],
            },
            "metrics": runner._empty_metrics(),
        }

    monkeypatch.setattr(native_cursor, "preflight", preflight)
    monkeypatch.setattr(native_cursor, "run_cursor", run)
    result = runner.run_exam(inputs()[0]["ulp"], config, "closed-book")
    assert result["status"] == ("failed" if drift else "ok")
    assert "managed:cursor-subscription" in json.dumps(adapters.validate_config(config))


def test_runner_preserves_verified_cursor_task_failure_identity_and_metrics(monkeypatch):
    config = {
        "schema": "zno-nmt.config.v1",
        "adapter": "cursor",
        "model": "cursor-grok-4.6-high",
        "effort": "high",
        "timeout_seconds": 20,
        "max_output_tokens": 100,
        "max_tool_calls": 1,
        "repeats": 1,
        "tools": [],
        "corpus_id": None,
        "provider": "managed:cursor-subscription",
    }
    fingerprints = {
        field: "a" * 64
        for field in (
            "binary_sha256",
            "native_config_sha256",
            "catalog_provider_sha256",
            "catalog_model_sha256",
            "settings_sha256",
            "request_shape_sha256",
        )
    }

    def preflight(checked, condition, sources_url):
        assert checked["adapter"] == "cursor"
        return {**fingerprints, "tool_schema_sha256": adapters.digest([]), "mcp_server_identity_sha256": None}

    def run(packet, checked, condition, **kwargs):
        return {
            "status": "failed",
            "failure_reason": CANDIDATE_RESPONSE_ERROR,
            "responses": {item["id"]: None for item in packet["items"]},
            "identity": {
                **fingerprints,
                "adapter": "cursor",
                "harness": "cursor-agent",
                "provider": "managed:cursor-subscription",
                "model": "cursor-grok-4.6-high",
                "requested_model": "cursor-grok-4.6-high",
                "requested_model_alias": "cursor-grok-4.6-high",
                "cli_version": "2026.09.10-fixture",
                "session_id": "verified-session",
                "effective_model": "unknown",
            },
            "metrics": {**runner._empty_metrics(), "tool_calls": 1, "total_tokens": 7},
        }

    monkeypatch.setattr(native_cursor, "preflight", preflight)
    monkeypatch.setattr(native_cursor, "run_cursor", run)
    result = runner.run_exam(inputs()[0]["ulp"], config, "closed-book")

    assert result["status"] == "failed"
    assert result["failure_reason"] == CANDIDATE_RESPONSE_ERROR
    assert result["identity"]["session_id"] == "verified-session"
    assert result["metrics"]["tool_calls"] == 1
    assert result["responses"] == {"q0001": None, "q0002": None}
