"""Explicit provider routing and local validation of JSON-object responses."""
import json

import pytest
from test_zno_nmt_runner import _config, _packet

from ukrainian_llm_eval import adapters

ROUTING = {"provider_endpoint": "fixture/endpoint", "expected_provider_name": "Fixture Provider",
           "reasoning_enabled": False}


def response(provider="Fixture Provider", responses=None):
    return {"model": "local-test-model", "provider": provider,
            "choices": [{"message": {"content": json.dumps({"responses": responses or {"opaque-1": "A"}})}}],
            "usage": {}}


def test_provider_pin_and_json_object_preserve_strict_local_answers(monkeypatch):
    monkeypatch.setenv("ZNO_NMT_ENDPOINT", "https://example.invalid/chat")
    captured = []

    def completion(_url, payload, **_kwargs):
        captured.append(payload)
        return response()

    monkeypatch.setattr(adapters, "_http_json", completion)
    result = adapters.run_chat_http(_packet(), _config(openrouter=ROUTING, http_response_format="json_object"),
                                    "closed-book", sources_url=None, prompt="Return JSON")
    assert captured[0]["provider"] == {"only": ["fixture/endpoint"], "allow_fallbacks": False,
                                       "require_parameters": True}
    assert captured[0]["reasoning"] == {"enabled": False}
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in captured[0]
    assert result["responses"] == {"opaque-1": "A"}
    assert result["identity"]["effective_provider"] == "Fixture Provider"
    assert result["identity"]["reasoning_enabled_requested"] is False


@pytest.mark.parametrize("provider", [None, "Fallback Provider"])
def test_missing_or_different_provider_is_rejected_without_retry(monkeypatch, provider):
    monkeypatch.setenv("ZNO_NMT_ENDPOINT", "https://example.invalid/chat")
    calls = []

    def completion(*_args, **_kwargs):
        calls.append(1)
        return response(provider)

    monkeypatch.setattr(adapters, "_http_json", completion)
    with pytest.raises(adapters.AdapterError, match="provider identity drift"):
        adapters.run_chat_http(_packet(), _config(openrouter=ROUTING), "closed-book",
                               sources_url=None, prompt="Return JSON")
    assert calls == [1]


def test_json_object_does_not_accept_foreign_question_ids(monkeypatch):
    monkeypatch.setenv("ZNO_NMT_ENDPOINT", "https://example.invalid/chat")
    monkeypatch.setattr(adapters, "_http_json", lambda *_a, **_k: response(responses={"foreign": "A"}))
    with pytest.raises(adapters.AdapterError):
        adapters.run_chat_http(_packet(), _config(http_response_format="json_object"), "closed-book",
                               sources_url=None, prompt="Return JSON")


def test_price_ceilings_reach_transport_with_provider_restrictions(monkeypatch):
    monkeypatch.setenv("ZNO_NMT_ENDPOINT", "https://example.invalid/chat")
    captured = []

    def completion(_url, payload, **_kwargs):
        captured.append(payload)
        return response()

    prices = {"prompt": "0.10", "completion": "0.34", "request": "0"}
    monkeypatch.setattr(adapters, "_http_json", completion)
    adapters.run_chat_http(_packet(), _config(openrouter={**ROUTING, "max_price": prices}),
                           "closed-book", sources_url=None, prompt="Return JSON")
    assert captured[0]["provider"] == {
        "only": ["fixture/endpoint"], "allow_fallbacks": False,
        "require_parameters": True, "max_price": prices,
    }


@pytest.mark.parametrize("prices", [
    {}, {"prompt": "0.1", "completion": "0.3"},
    {"prompt": "NaN", "completion": "0.3", "request": "0"},
    {"prompt": "-1", "completion": "0.3", "request": "0"},
    {"prompt": True, "completion": "0.3", "request": "0"},
    {"prompt": "0.1", "completion": "0.3", "request": "0", "image": "0"},
])
def test_invalid_price_ceilings_rejected_before_transport(prices):
    with pytest.raises(adapters.AdapterError, match="price"):
        adapters.validate_config(_config(openrouter={**ROUTING, "max_price": prices}))


@pytest.mark.parametrize("change", [
    {"http_response_format": []}, {"http_response_format": "text"},
    {"openrouter": {**ROUTING, "allow_fallbacks": True}},
    {"openrouter": {**ROUTING, "reasoning_enabled": 0}},
    {"openrouter": {**ROUTING, "provider_endpoint": ""}},
    {"openrouter": ROUTING, "effort": "high"},
])
def test_invalid_http_controls_rejected(change):
    with pytest.raises(adapters.AdapterError):
        adapters.validate_config(_config(**change))
