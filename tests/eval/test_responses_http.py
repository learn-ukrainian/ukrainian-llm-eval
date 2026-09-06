"""Deterministic boundaries for the DeepSeek Responses HTTP adapter."""

from __future__ import annotations

import copy
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from ukrainian_llm_eval import adapters, responses_http

MappingOrBytes = dict[str, Any] | bytes


def _packet() -> dict[str, Any]:
    return {
        "schema": "zno-nmt.questions.v1",
        "packet_sha256": "a" * 64,
        "items": [
            {
                "id": "opaque-1",
                "kind": "single",
                "question": "Питання",
                "options": [{"id": "A", "text": "Варіант"}],
                "rows": [],
            }
        ],
    }


def _config(**extra: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "schema": "zno-nmt.config.v1",
        "adapter": "responses-http",
        "model": "deepseek-v4-flash",
        "effort": "high",
        "timeout_seconds": 15,
        "max_output_tokens": 100,
        "max_tool_calls": 2,
        "repeats": 1,
        "tools": ["verify_word"],
        "corpus_id": "fixture-corpus",
        "provider": "deepseek",
        "endpoint_env": "ZNO_NMT_RESPONSES_ENDPOINT",
        "key_env": "ZNO_NMT_RESPONSES_KEY",
        "http_response_format": "json_schema",
    }
    config.update(extra)
    return config


def _usage(input_tokens: int = 11, output_tokens: int = 5) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 2},
        "total_tokens": input_tokens + output_tokens,
    }


def _message_body(
    *,
    response_id: str = "resp-1",
    model: str = "deepseek-v4-flash",
    object_type: str = "response",
    text: str | None = None,
    output: list[dict[str, Any]] | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    if text is None:
        text = json.dumps({"responses": {"opaque-1": "A"}})
    if output is None:
        output = [
            {
                "type": "reasoning",
                "id": "rs-1",
                "status": "completed",
                "content": [{"type": "reasoning_text", "text": "Перевіряю."}],
            },
            {
                "type": "message",
                "id": "msg-1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        ]
    return {
        "id": response_id,
        "object": object_type,
        "status": status,
        "error": None,
        "incomplete_details": None,
        "model": model,
        "output": output,
        "usage": _usage(),
        "store": False,
        "previous_response_id": None,
    }


def test_closed_book_builds_explicit_stateless_request_and_extracts_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZNO_NMT_RESPONSES_ENDPOINT", "https://example.invalid/responses")
    monkeypatch.setenv("ZNO_NMT_RESPONSES_KEY", "secret-not-in-payload")
    captured: dict[str, Any] = {}

    def completion(_url: str, payload: MappingOrBytes, **kwargs: Any) -> dict[str, Any]:
        captured.update(payload=payload, key=kwargs["key"])
        return _message_body()

    monkeypatch.setattr(responses_http.adapters, "_http_json", completion)
    result = responses_http.run_responses_http(
        _packet(),
        _config(),
        "closed-book",
        sources_url=None,
        prompt="Відповідай лише JSON.",
    )

    payload = captured["payload"]
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["input"] == [{"role": "user", "content": "Відповідай лише JSON."}]
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["max_output_tokens"] == 100
    assert payload["stream"] is False
    assert payload["store"] is False
    assert payload["tool_choice"] == "none"
    assert "tools" not in payload
    assert "previous_response_id" not in payload
    assert "conversation" not in payload
    assert payload["text"]["format"]["type"] == "json_schema"
    assert captured["key"] == "secret-not-in-payload"
    assert "secret-not-in-payload" not in json.dumps(payload)
    assert result["responses"] == {"opaque-1": "A"}
    assert result["identity"]["effective_model"] == "deepseek-v4-flash"
    assert result["identity"]["effective_effort"] == "unknown"
    assert result["identity"]["store_requested"] is False
    assert result["identity"]["previous_response_id_used"] is False
    assert result["metrics"] == {
        "elapsed_seconds": result["metrics"]["elapsed_seconds"],
        "input_tokens": 11,
        "output_tokens": 5,
        "total_tokens": 16,
        "cost_usd": None,
        "tool_calls": 0,
    }

def test_sources_preserves_reasoning_and_function_call_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZNO_NMT_RESPONSES_ENDPOINT", "https://example.invalid/responses")
    requests: list[dict[str, Any] | bytes] = []
    broker_calls: list[tuple[str, MappingOrBytes]] = []
    responses = iter(
        [
            _message_body(
                response_id="resp-1",
                output=[
                    {
                        "type": "reasoning",
                        "id": "rs-1",
                        "status": "completed",
                        "content": [{"type": "reasoning_text", "text": "Шукаю."}],
                        "summary": [],
                    },
                    {
                        "type": "function_call",
                        "id": "fc-1",
                        "status": "completed",
                        "call_id": "call-1",
                        "name": "mcp__sources__verify_word",
                        "arguments": '{"word":"слово"}',
                    },
                ],
            ),
            _message_body(response_id="resp-2"),
        ]
    )

    monkeypatch.setattr(
        responses_http.adapters,
        "_mcp_list_tools",
        lambda *_args: ([{"name": "verify_word", "inputSchema": {"type": "object"}}], "mcp-id"),
    )

    def broker(_url: str, name: str, arguments: MappingOrBytes, _timeout: int) -> dict[str, Any]:
        broker_calls.append((name, arguments))
        return {"valid": True, "word": "слово"}

    monkeypatch.setattr(responses_http.adapters, "_mcp_call", broker)

    def completion(_url: str, payload: MappingOrBytes, **_kwargs: Any) -> dict[str, Any]:
        requests.append(copy.deepcopy(payload))
        return next(responses)

    monkeypatch.setattr(responses_http.adapters, "_http_json", completion)
    result = responses_http.run_responses_http(
        _packet(),
        _config(),
        "sources",
        sources_url="https://sources.invalid/mcp",
        prompt="Іспит.",
    )

    assert len(requests) == 2
    first = requests[0]
    second = requests[1]
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["tools"] == [
        {
            "type": "function",
            "name": "mcp__sources__verify_word",
            "description": "Approved Sources reference tool",
            "parameters": {"type": "object"},
        }
    ]
    assert first["tool_choice"] == "auto"
    assert second["input"][0] == {"role": "user", "content": "Іспит."}
    assert second["input"][1]["type"] == "reasoning"
    assert second["input"][1]["content"] == [{"type": "reasoning_text", "text": "Шукаю."}]
    assert second["input"][1]["summary"] == []
    assert second["input"][2]["type"] == "function_call"
    assert second["input"][2]["call_id"] == "call-1"
    assert second["input"][3] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": '{"valid":true,"word":"слово"}',
    }
    assert broker_calls == [("mcp__sources__verify_word", {"word": "слово"})]
    assert result["responses"] == {"opaque-1": "A"}
    assert result["metrics"]["tool_calls"] == 1
    assert result["identity"]["mcp_server_identity_sha256"] == "mcp-id"


class _Budget:
    output_parameter_name = "max_output_tokens"

    def __init__(self) -> None:
        self.committed: list[dict[str, Any]] = []
        self.observed: list[tuple[Any, int]] = []
        self.tool_results: list[Any] = []

    def commit_request(self, payload: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
        self.committed.append(copy.deepcopy(payload))
        return adapters.canonical(payload).encode("utf-8") + b"\n", {"round": len(self.committed)}

    def observe(self, usage: MappingOrBytes, *, tool_calls: int) -> dict[str, Any]:
        self.observed.append((copy.deepcopy(usage), tool_calls))
        return {"round": len(self.observed), "tool_calls": tool_calls}

    def serialize_tool_result(self, value: Any) -> str:
        self.tool_results.append(copy.deepcopy(value))
        return adapters.canonical(value)


def test_budget_is_committed_and_observed_for_each_complete_history_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZNO_NMT_RESPONSES_ENDPOINT", "https://example.invalid/responses")
    monkeypatch.setattr(
        responses_http.adapters,
        "_mcp_list_tools",
        lambda *_args: ([{"name": "verify_word", "inputSchema": {"type": "object"}}], None),
    )
    monkeypatch.setattr(
        responses_http.adapters,
        "_mcp_call",
        lambda *_args: {"ok": True},
    )
    calls = iter(
        [
            _message_body(
                response_id="resp-1",
                output=[
                    {
                        "type": "function_call",
                        "id": "fc-1",
                        "status": "completed",
                        "call_id": "call-1",
                        "name": "mcp__sources__verify_word",
                        "arguments": "{}",
                    }
                ],
            ),
            _message_body(response_id="resp-2"),
        ]
    )
    monkeypatch.setattr(responses_http.adapters, "_http_json", lambda *_args, **_kwargs: next(calls))
    budget = _Budget()
    responses_http.run_responses_http(
        _packet(),
        _config(),
        "sources",
        sources_url="https://sources.invalid/mcp",
        prompt="Іспит.",
        request_budget=budget,
    )
    assert len(budget.committed) == 2
    assert len(budget.observed) == 2
    assert budget.observed[0][1] == 1
    assert budget.observed[1][1] == 0
    assert budget.committed[1]["input"][1]["type"] == "function_call"
    assert budget.committed[1]["input"][2]["type"] == "function_call_output"
    assert budget.tool_results == [{"ok": True}]


def test_closed_book_function_call_is_rejected_without_broker_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZNO_NMT_RESPONSES_ENDPOINT", "https://example.invalid/responses")
    called = False

    def broker(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(responses_http.adapters, "_mcp_call", broker)
    body = _message_body(
        output=[
            {
                "type": "function_call",
                "id": "fc-1",
                "status": "completed",
                "call_id": "call-1",
                "name": "mcp__sources__verify_word",
                "arguments": "{}",
            }
        ]
    )
    monkeypatch.setattr(responses_http.adapters, "_http_json", lambda *_args, **_kwargs: body)
    with pytest.raises(responses_http.AdapterError, match="tool_policy_error"):
        responses_http.run_responses_http(
            _packet(), _config(), "closed-book", sources_url=None, prompt="Іспит."
        )
    assert called is False


def test_response_storage_and_duplicate_function_call_identity_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZNO_NMT_RESPONSES_ENDPOINT", "https://example.invalid/responses")
    storage_body = _message_body()
    storage_body["store"] = True
    monkeypatch.setattr(responses_http.adapters, "_http_json", lambda *_args, **_kwargs: storage_body)
    with pytest.raises(responses_http.AdapterError, match="storage"):
        responses_http.run_responses_http(
            _packet(), _config(), "closed-book", sources_url=None, prompt="Іспит."
        )

    duplicate_body = _message_body(
        response_id="resp-1",
        output=[
            {
                "type": "function_call",
                "id": "fc-1",
                "status": "completed",
                "call_id": "call-1",
                "name": "mcp__sources__verify_word",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "id": "fc-2",
                "status": "completed",
                "call_id": "call-1",
                "name": "mcp__sources__verify_word",
                "arguments": "{}",
            },
        ],
    )
    monkeypatch.setattr(responses_http.adapters, "_http_json", lambda *_args, **_kwargs: duplicate_body)
    monkeypatch.setattr(
        responses_http.adapters,
        "_mcp_list_tools",
        lambda *_args: ([{"name": "verify_word", "inputSchema": {"type": "object"}}], None),
    )
    with pytest.raises(responses_http.AdapterError, match="duplicated"):
        responses_http.run_responses_http(
            _packet(), _config(), "sources", sources_url="https://sources.invalid/mcp", prompt="Іспит."
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"model": "other-model"}, "model drift"),
        ({"status": "incomplete"}, "final status is not completed"),
        ({"status": "in_progress"}, "final status is not completed"),
        ({"object_type": "chat.completion"}, "object type"),
    ],
)
def test_response_identity_and_terminal_status_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any], message: str
) -> None:
    monkeypatch.setenv("ZNO_NMT_RESPONSES_ENDPOINT", "https://example.invalid/responses")
    body = _message_body(**change)
    monkeypatch.setattr(responses_http.adapters, "_http_json", lambda *_args, **_kwargs: body)
    with pytest.raises(responses_http.AdapterError, match=message):
        responses_http.run_responses_http(
            _packet(), _config(), "closed-book", sources_url=None, prompt="Іспит."
        )


@pytest.mark.parametrize(
    "change",
    [
        {"http_response_format": []},
        {"http_response_format": "text"},
        {"effort": []},
        {"effort": "ultra"},
        {"tools": ["shell"]},
        {"endpoint_env": "bad-name!"},
    ],
)
def test_invalid_responses_runtime_config_is_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(responses_http.AdapterError):
        responses_http.run_responses_http(
            _packet(), _config(**change), "closed-book", sources_url=None, prompt="Іспит."
        )


def test_evidence_receives_request_response_and_tool_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZNO_NMT_RESPONSES_ENDPOINT", "https://example.invalid/responses")
    events: list[str] = []
    monkeypatch.setattr(
        responses_http.adapters,
        "_http_json",
        lambda *_args, **_kwargs: _message_body(),
    )
    responses_http.run_responses_http(
        _packet(), _config(), "closed-book", sources_url=None, prompt="Іспит.",
        evidence=lambda kind, _value: events.append(kind),
    )
    assert events == ["completion_request", "completion_response"]


def test_local_http_transport_preserves_raw_response_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    response = _message_body()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            seen.update(
                path=self.path,
                authorization=self.headers.get("Authorization"),
                payload=json.loads(self.rfile.read(length)),
            )
            raw = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    events: list[tuple[str, Any]] = []
    try:
        monkeypatch.setenv(
            "ZNO_NMT_RESPONSES_ENDPOINT",
            f"http://127.0.0.1:{server.server_port}/responses",
        )
        monkeypatch.setenv("ZNO_NMT_RESPONSES_KEY", "transport-secret")
        result = responses_http.run_responses_http(
            _packet(),
            _config(http_response_format="json_object"),
            "closed-book",
            sources_url=None,
            prompt="Іспит.",
            evidence=lambda kind, value: events.append((kind, value)),
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert result["responses"] == {"opaque-1": "A"}
    assert seen["path"] == "/responses"
    assert seen["authorization"] == "Bearer transport-secret"
    assert seen["payload"]["text"] == {"format": {"type": "json_object"}}
    raw_event = next(value for kind, value in events if kind == "completion_body")
    assert raw_event["truncated"] is False
    assert raw_event["base64"]
