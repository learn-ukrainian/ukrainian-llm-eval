import json

import pytest
from test_research_scheduling import inputs

from ukrainian_llm_eval import adapters, native_kimi, runner


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
