"""Public runner wiring for the native Codex adapter, with no live CLI call."""

from __future__ import annotations

import json

import pytest
from test_research_scheduling import inputs

from ukrainian_llm_eval import adapters, native_codex, runner
from ukrainian_llm_eval.candidate_outcome import CANDIDATE_RESPONSE_ERROR


def _config() -> dict[str, object]:
    return {
        "schema": "zno-nmt.config.v1", "adapter": "codex", "model": "gpt-5.6-luna", "effort": "high",
        "timeout_seconds": 20, "max_output_tokens": 100, "max_tool_calls": 1, "repeats": 1,
        "tools": [], "corpus_id": None, "provider": "managed:codex-subscription",
    }


def _fingerprints() -> dict[str, str]:
    hashes = {field: "a" * 64 for field in (
        "entrypoint_sha256", "native_runtime_sha256", "control_receipt_sha256",
        "settings_sha256", "request_shape_sha256",
    )}
    return {**hashes, "cli_version": "codex-cli fixture"}


@pytest.mark.parametrize("drift", [False, True])
def test_runner_dispatches_codex_with_private_runtime_path_and_checks_drift(monkeypatch, tmp_path, drift):
    config = _config()
    private = str(tmp_path / "operator-provisioning")
    monkeypatch.setenv("UKRAINIAN_LLM_EVAL_CODEX_PROVISIONING_DIR", private)
    fingerprints = _fingerprints()
    seen: list[str] = []

    def preflight(checked, condition, sources_url, *, private_env_path):
        seen.append(private_env_path)
        assert checked["adapter"] == "codex"
        assert condition == "closed-book" and sources_url is None
        return {**fingerprints, "tool_schema_sha256": adapters.digest([]), "mcp_server_identity_sha256": None}

    def run(packet, checked, condition, *, private_env_path, **_kwargs):
        seen.append(private_env_path)
        return {
            "responses": {item["id"]: "A" for item in packet["items"]},
            "identity": {
                **fingerprints,
                "session_id": "native-session",
                "effective_model": "unknown",
                "cli_version": "codex-cli fixture",
                "native_runtime_sha256": "b" * 64 if drift else fingerprints["native_runtime_sha256"],
            },
            "metrics": runner._empty_metrics(),
        }

    monkeypatch.setattr(native_codex, "preflight", preflight)
    monkeypatch.setattr(native_codex, "run_codex", run)
    result = runner.run_exam(inputs()[0]["ulp"], config, "closed-book")

    assert seen == [private, private]
    assert result["status"] == ("failed" if drift else "ok")
    assert private not in json.dumps(result)
    assert private not in json.dumps(adapters.validate_config(config))


def test_runner_preserves_verified_codex_candidate_failure(monkeypatch):
    config = _config()
    fingerprints = _fingerprints()
    candidate_hashes = {"response_schema_sha256": "b" * 64}
    monkeypatch.setenv("UKRAINIAN_LLM_EVAL_CODEX_PROVISIONING_DIR", "/private/operator-path")

    monkeypatch.setattr(
        native_codex,
        "preflight",
        lambda *_args, **_kwargs: {
            **fingerprints, "tool_schema_sha256": adapters.digest([]), "mcp_server_identity_sha256": None,
        },
    )

    def run(packet, *_args, **_kwargs):
        return {
            "status": "failed",
            "failure_reason": CANDIDATE_RESPONSE_ERROR,
            "responses": {item["id"]: None for item in packet["items"]},
            "identity": {
                **fingerprints,
                **candidate_hashes,
                "adapter": "codex",
                "harness": "codex-cli",
                "provider": "managed:codex-subscription",
                "model": "gpt-5.6-luna",
                "requested_model": "gpt-5.6-luna",
                "requested_model_alias": "gpt-5.6-luna",
                "cli_version": "codex-cli fixture",
                "session_id": "verified-session",
                "effective_model": "unknown",
            },
            "metrics": {**runner._empty_metrics(), "tool_calls": 0, "input_tokens": 3, "output_tokens": 4},
        }

    monkeypatch.setattr(native_codex, "run_codex", run)
    result = runner.run_exam(inputs()[0]["ulp"], config, "closed-book")

    assert result["status"] == "failed"
    assert result["failure_reason"] == CANDIDATE_RESPONSE_ERROR
    assert result["identity"]["session_id"] == "verified-session"
    assert result["metrics"]["input_tokens"] == 3
    assert result["responses"] == {"q0001": None, "q0002": None}


def test_runner_rejects_codex_candidate_failure_without_native_receipt(monkeypatch):
    config = _config()
    monkeypatch.setattr(
        native_codex,
        "preflight",
        lambda *_args, **_kwargs: {"tool_schema_sha256": adapters.digest([]), "mcp_server_identity_sha256": None},
    )
    monkeypatch.setattr(
        native_codex,
        "run_codex",
        lambda *_args, **_kwargs: {
            "status": "failed",
            "failure_reason": CANDIDATE_RESPONSE_ERROR,
            "responses": {"q0001": None, "q0002": None},
            "identity": {"session_id": "unverified"},
            "metrics": {"tool_calls": 0},
        },
    )

    result = runner.run_exam(inputs()[0]["ulp"], config, "closed-book")

    assert result["status"] == "failed"
    assert result["failure_reason"] == "response_error"
    assert result["identity"]["session_id"] is None
    assert result["metrics"] == runner._empty_metrics()
