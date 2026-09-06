import json

import pytest
from test_research_scheduling import inputs
from test_responses_http import _config, _message_body

from ukrainian_llm_eval import adapters, runner
from ukrainian_llm_eval.execution import route_fingerprint


def test_responses_dispatch_binds_endpoint_and_runs_validated_packet(monkeypatch):
    config = _config(tools=[], corpus_id=None)
    packet = inputs()[0]["ulp"]
    expected = {item["id"]: "A" for item in packet["items"]}
    endpoint = "https://example.invalid/private-responses"
    monkeypatch.setenv(config["endpoint_env"], endpoint)
    first = route_fingerprint(config, None)
    monkeypatch.setenv(config["endpoint_env"], endpoint + "-changed")
    assert route_fingerprint(config, None) != first
    monkeypatch.setenv(config["endpoint_env"], endpoint)
    sent = []

    def transport(url, payload, **kwargs):
        sent.append(payload)
        assert url == endpoint
        return _message_body(text=json.dumps({"responses": expected}))

    monkeypatch.setattr(adapters, "_http_json", transport)
    result = runner.run_exam(packet, config, "closed-book")
    assert result["status"] == "ok"
    assert result["responses"] == expected
    assert len(sent) == 1 and "input" in sent[0] and "messages" not in sent[0]
    assert sent[0]["max_output_tokens"] == config["max_output_tokens"]
    assert endpoint not in json.dumps(result)


def test_responses_unsupported_effort_rejected_before_endpoint_or_transport(monkeypatch):
    config = _config(effort="ultra")
    monkeypatch.delenv(config["endpoint_env"], raising=False)
    with pytest.raises(adapters.AdapterError, match="Responses effort is unsupported"):
        adapters.validate_config(config)
    with pytest.raises(adapters.AdapterError, match="Responses effort is unsupported"):
        adapters.preflight(config, "closed-book")
