"""Boundary tests for the evaluator's provider runner and adapters."""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from ukrainian_llm_eval import adapters, runner
from ukrainian_llm_eval.core import prepare_exam
from ukrainian_llm_eval.mcp_proxy import Bridge


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
        "adapter": "chat-http",
        "model": "local-test-model",
        "effort": None,
        "timeout_seconds": 15,
        "max_output_tokens": 100,
        "max_tool_calls": 2,
        "repeats": 1,
        "tools": ["verify_word"],
        "corpus_id": "fixture-corpus",
        "endpoint_env": "ZNO_NMT_ENDPOINT",
        "key_env": "ZNO_NMT_KEY",
    }
    config.update(extra)
    return config


def test_config_is_exact_and_sources_tools_are_proxy_allowlist() -> None:
    assert adapters.validate_config(_config())["tools"] == ["verify_word"]
    with pytest.raises(adapters.AdapterError, match="unsupported fields"):
        adapters.validate_config(_config(untrusted="value"))
    with pytest.raises(adapters.AdapterError, match="allowlisted"):
        adapters.validate_config(_config(tools=["search_external"]))
    with pytest.raises(adapters.AdapterError, match="allowlisted"):
        adapters.validate_config(_config(tools=["mcp__sources__verify_word"]))


def test_mcp_clients_reject_redirects_without_forwarding_session_header() -> None:
    seen = {
        "source_requests": 0,
        "source_sessions": [],
        "destination_requests": 0,
        "destination_methods": [],
        "destination_sessions": [],
    }

    class DestinationHandler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            seen["destination_requests"] += 1
            seen["destination_methods"].append(self.command)
            seen["destination_sessions"].append(self.headers.get("Mcp-Session-Id"))

        def do_GET(self) -> None:
            self._record()
            self.send_response(HTTPStatus.OK)
            self.end_headers()

        def do_POST(self) -> None:
            self._record()
            self.send_response(HTTPStatus.OK)
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            return

    destination_server = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path == "/redirect":
                seen["source_requests"] += 1
                seen["source_sessions"].append(self.headers.get("Mcp-Session-Id"))
                self.send_response(HTTPStatus.FOUND)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{destination_server.server_port}/destination",
                )
                self.end_headers()
                return
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            return

    source_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    servers = [source_server, destination_server]
    server_threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in servers
    ]
    for server_thread in server_threads:
        server_thread.start()
    url = f"http://127.0.0.1:{source_server.server_port}/redirect"
    try:
        with pytest.raises(adapters.AdapterError, match="MCP request failed"):
            adapters._mcp_request(url, {}, timeout=2, session_id="session-secret")

        bridge = Bridge(url, ["verify_word"])
        bridge.headers["Mcp-Session-Id"] = "session-secret"
        with pytest.raises(urllib.error.HTTPError) as redirect:
            bridge.request("tools/list", {})
        assert redirect.value.code == HTTPStatus.FOUND
    finally:
        for server in servers:
            server.shutdown()
        for server_thread in server_threads:
            server_thread.join(timeout=2)
        for server in servers:
            server.server_close()

    assert seen == {
        "source_requests": 2,
        "source_sessions": ["session-secret", "session-secret"],
        "destination_requests": 0,
        "destination_methods": [],
        "destination_sessions": [],
    }


def test_http_malicious_tool_never_reaches_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZNO_NMT_ENDPOINT", "https://example.invalid/chat")
    monkeypatch.setenv("ZNO_NMT_KEY", "secret-never-in-prompt")
    monkeypatch.setattr(adapters, "_mcp_list_tools", lambda *_args: ([{"name": "verify_word", "inputSchema": {"type": "object"}}], "server"))
    called = False

    def broker(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("malicious call reached MCP broker")

    monkeypatch.setattr(adapters, "_mcp_call", broker)
    monkeypatch.setattr(
        adapters,
        "_http_json",
        lambda *_args, **_kwargs: {
            "model": "local-test-model",
            "choices": [{"message": {"tool_calls": [{"id": "call-1", "function": {"name": "mcp__sources__shell", "arguments": "{}"}}]}}],
        },
    )
    with pytest.raises(adapters.AdapterError, match="tool_policy_error"):
        adapters.run_chat_http(_packet(), _config(), "sources", sources_url="https://sources.invalid/mcp", prompt="exam")
    assert not called


def test_closed_book_http_sends_no_tools_or_runtime_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZNO_NMT_ENDPOINT", "https://example.invalid/chat")
    monkeypatch.setenv("ZNO_NMT_KEY", "secret-never-in-prompt")
    captured: dict[str, Any] = {}

    def completion(_url: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        captured.update(payload=payload, key=kwargs["key"])
        return {
            "model": "local-test-model",
            "choices": [{"message": {"content": json.dumps({"responses": {"opaque-1": "A"}})}}],
            "usage": {},
        }

    monkeypatch.setattr(adapters, "_http_json", completion)
    result = adapters.run_chat_http(_packet(), _config(), "closed-book", sources_url="https://sources.invalid/mcp", prompt="exam")
    assert "tools" not in captured["payload"]
    assert "secret-never-in-prompt" not in json.dumps(captured["payload"])
    assert captured["key"] == "secret-never-in-prompt"
    assert result["metrics"]["cost_usd"] is None
    assert result["metrics"]["input_tokens"] is None


def test_claude_empty_builtins_and_model_drift_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(
        adapter="claude",
        endpoint_env=None,
        key_env=None,
        claude_bin="claude-fixture",
    )
    # The strict schema does not permit HTTP-only fields on the native adapter.
    del config["endpoint_env"]
    del config["key_env"]
    monkeypatch.setattr(adapters, "_claude_capabilities", lambda *_args, **_kwargs: ("claude-fixture", "2.1.fixture"))
    observed: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.update(argv=argv, env=kwargs["env"], cwd=kwargs["cwd"], input=kwargs["prompt"])
        stdout = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init", "model": "wrong-model", "tools": []}),
                json.dumps({"type": "result", "result": json.dumps({"responses": {"opaque-1": "A"}})}),
            ]
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="provider output must not be retained")

    monkeypatch.setattr(adapters, "_run_claude_process", fake_run)
    with pytest.raises(adapters.AdapterError, match="model drift"):
        adapters.run_claude(_packet(), config, "closed-book", sources_url=None, prompt="exam")
    assert observed["argv"][observed["argv"].index("--tools") + 1] == ""
    assert "--strict-mcp-config" in observed["argv"]
    assert observed["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "100"
    assert "CLAUDE_CODE_SIMPLE" not in observed["env"]
    assert "secret-never-in-prompt" not in observed["input"]


@pytest.mark.parametrize("sessions", [(None, None), ("first", None), ("first", "second")])
def test_claude_native_session_requires_consistent_terminal_identity(sessions) -> None:
    events = [{"type": "system", "session_id": sessions[0]},
              {"type": "result", "session_id": sessions[1]}]
    with pytest.raises(adapters.AdapterError, match="session identity"):
        adapters._claude_session_identity("\n".join(map(json.dumps, events)))


def test_claude_native_session_does_not_invent_fresh_identity() -> None:
    stream = "\n".join(map(json.dumps, [{"type": "system", "session_id": "native-session"},
                                       {"type": "result", "session_id": "native-session"}]))
    assert adapters._claude_session_identity(stream) == "native-session"
    assert adapters._claude_session_identity(stream) == "native-session"


def test_claude_child_env_keeps_local_keychain_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", "fixture-user")
    monkeypatch.setenv("LOGNAME", "fixture-login")
    environment = adapters._child_env(100)
    assert environment["USER"] == "fixture-user"
    assert environment["LOGNAME"] == "fixture-login"
    assert "CLAUDE_CODE_SIMPLE" not in environment


def test_structured_output_tool_is_not_counted_as_retrieval() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "model": "fixture", "tools": ["StructuredOutput"]}),
            json.dumps({"type": "tool_use", "name": "StructuredOutput"}),
            json.dumps({"type": "result", "structured_output": {"responses": {"opaque-1": "A"}}}),
        ]
    )
    responses, model, calls, _usage = adapters._parse_stream_json(stdout, _packet(), set(), 1)
    assert responses == {"opaque-1": "A"}
    assert model == "fixture"
    assert calls == 0


def test_preflight_does_not_export_endpoint_or_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZNO_NMT_ENDPOINT", "https://hidden.invalid/v1/chat/completions")
    monkeypatch.setenv("ZNO_NMT_KEY", "private-key")
    capability = adapters.preflight(_config(), "closed-book")
    rendered = json.dumps(capability)
    assert "hidden.invalid" not in rendered
    assert "private-key" not in rendered
    assert capability["capability"] == "chat-http-controller-mediated"


def test_mcp_identity_hashes_actual_safe_corpus_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = {
        "server_code_sha256": "a" * 64,
        "sources_db_sha256": "b" * 64,
        "sources_db_bytes": 123,
        "vesum_db_sha256": "c" * 64,
        "vesum_db_bytes": 456,
    }
    calls: list[str] = []
    replies = iter(
        [
            {"result": {"serverInfo": {"name": "Sources"}}},
            {},
            {"result": {"tools": [
                {"name": "verify_word", "inputSchema": {"type": "object"}},
                {"name": "mcp_server_identity", "inputSchema": {"type": "object"}},
            ]}},
            {"result": {"content": [{"type": "text", "text": json.dumps(identity)}]}},
        ]
    )

    def request(_url: str, payload: dict[str, Any], _timeout: int, _session: str | None = None):
        calls.append(payload["method"])
        return next(replies), "session"

    monkeypatch.setattr(adapters, "_mcp_request", request)
    tools, identity_sha = adapters._mcp_list_tools("https://sources.invalid/mcp", 5)
    assert [tool["name"] for tool in tools] == ["verify_word", "mcp_server_identity"]
    assert identity_sha == adapters.digest(identity)
    assert calls == ["initialize", "notifications/initialized", "tools/list", "tools/call"]


def test_mcp_missing_identity_is_unknown_not_serverinfo_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = iter(
        [
            {"result": {"serverInfo": {"name": "Sources", "version": "different"}}},
            {},
            {"result": {"tools": [{"name": "verify_word", "inputSchema": {"type": "object"}}]}},
        ]
    )
    monkeypatch.setattr(adapters, "_mcp_request", lambda *_args: (next(replies), "session"))
    _tools, identity_sha = adapters._mcp_list_tools("https://sources.invalid/mcp", 5)
    assert identity_sha is None


def test_provider_json_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    with pytest.raises(adapters.AdapterError, match="duplicate"):
        adapters._strict_json_loads('{"responses":{},"responses":{}}')
    with pytest.raises(adapters.AdapterError, match="non-finite"):
        adapters._strict_json_loads('{"value":NaN}')


def test_sources_prompt_discloses_exact_call_cap_only_for_sources() -> None:
    sources_prompt = adapters.build_prompt(_packet(), "sources", max_tool_calls=20)
    closed_prompt = adapters.build_prompt(_packet(), "closed-book", max_tool_calls=20)
    assert "at most 20 total reference-tool calls" in sources_prompt
    assert "including failed attempts" in sources_prompt
    assert "submit answers without further calls" in sources_prompt
    assert "reference-tool calls" not in closed_prompt


def test_tool_policy_and_limit_failures_have_distinct_safe_reasons() -> None:
    policy_stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "model": "fixture", "tools": ["mcp__sources__verify_word"]}),
            json.dumps({"type": "tool_use", "name": "mcp__sources__not_allowed"}),
        ]
    )
    limit_stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "model": "fixture", "tools": ["mcp__sources__verify_word"]}),
            json.dumps({"type": "tool_use", "name": "mcp__sources__verify_word"}),
            json.dumps({"type": "tool_use", "name": "mcp__sources__verify_word"}),
        ]
    )
    with pytest.raises(adapters.AdapterError, match="tool_policy_error") as policy:
        adapters._parse_stream_json(policy_stream, _packet(), {"mcp__sources__verify_word"}, 1)
    with pytest.raises(adapters.AdapterError, match="tool_limit_error") as limit:
        adapters._parse_stream_json(limit_stream, _packet(), {"mcp__sources__verify_word"}, 1)
    assert adapters.normalized_reason(policy.value) == "tool_policy_error"
    assert adapters.normalized_reason(limit.value) == "tool_limit_error"


def test_response_schema_closes_every_object_and_preserves_item_kinds() -> None:
    packet = {
        "items": [
            {"id": "single", "kind": "single", "rows": []},
            {
                "id": "matching",
                "kind": "matching",
                "rows": [{"id": "row-1"}, {"id": "row-2"}],
            },
        ]
    }
    schema = adapters.response_schema(packet)

    def assert_closed(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for child in value.values():
                assert_closed(child)
        elif isinstance(value, list):
            for child in value:
                assert_closed(child)

    assert_closed(schema)
    responses = schema["properties"]["responses"]["properties"]
    assert responses["single"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    matching = responses["matching"]["anyOf"][0]
    assert matching["required"] == ["row-1", "row-2"]
    assert set(matching["properties"]) == {"row-1", "row-2"}
    assert "correct" not in json.dumps(schema)


def test_runner_retains_preflight_hashes_without_provider_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    exam = {
        "schema": "zno-nmt.exam.v1", "title": "fixture", "subject": "Ukrainian", "year": 2022,
        "provenance": {"source_url": "https://example.invalid", "source_revision": "fixture", "license": "test", "exposure": "public"},
        "scoring": {"kind": "benchmark", "policy_url": None, "pass_threshold": None, "expected_items": 1, "expected_points": 1},
        "items": [{"id": "1", "kind": "single", "question": "Питання", "options": [{"id": "A", "text": "Варіант"}], "rows": [], "correct": "A"}],
    }
    packet, _key = prepare_exam(exam)
    cap = {"tool_schema_sha256": "b" * 64, "mcp_server_identity_sha256": "c" * 64}
    monkeypatch.setattr(runner, "preflight", lambda *_args, **_kwargs: cap)
    prompt_args: dict[str, Any] = {}

    def build_prompt(*_args: Any, **kwargs: Any) -> str:
        prompt_args.update(kwargs)
        return "exam"

    monkeypatch.setattr(runner.adapters, "build_prompt", build_prompt)
    trial = {
        "responses": {"q0001": "A"},
        "identity": {"adapter": "chat-http", "harness": "chat-http", "model": "local-test-model", "effective_effort": "unknown"},
        "metrics": {"elapsed_seconds": 1.0, "input_tokens": None, "output_tokens": None, "total_tokens": None, "cost_usd": None, "tool_calls": 0},
    }
    monkeypatch.setattr(runner.adapters, "run_chat_http", lambda *_args, **_kwargs: trial)
    result = runner.run_exam(packet, _config(), "closed-book")
    assert result["status"] == "ok"
    assert result["identity"]["preflight_tool_schema_sha256"] == "b" * 64
    assert "provider output" not in json.dumps(result)
    assert result["comparison"]["constants_sha256"]
    assert prompt_args["max_tool_calls"] == 2


def test_native_timeout_kills_its_whole_process_group(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    seen: dict[str, Any] = {}

    class TimedOutProcess:
        pid = 321
        returncode = -9

        def communicate(self, _prompt: str | None = None, *, timeout: int | None = None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("claude", timeout)
            return "partial output", "partial diagnostics"

        def kill(self) -> None:
            raise AssertionError("process-group kill should be preferred")

    def fake_popen(*_args: Any, **kwargs: Any) -> TimedOutProcess:
        seen.update(kwargs)
        return TimedOutProcess()

    monkeypatch.setattr(adapters.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(adapters.os, "killpg", lambda pid, sig: seen.update(kill=(pid, sig)))
    with pytest.raises(adapters.AdapterError, match="timeout"):
        adapters._run_claude_process(["claude"], cwd=tmp_path, env={}, prompt="exam", timeout=1,
                                    evidence=lambda kind, payload: seen.update(evidence=(kind, payload)))
    assert seen["start_new_session"] is True
    assert seen["kill"] == (321, adapters.signal.SIGKILL)
    assert seen["evidence"] == ("cli_timeout", {"stdout": "partial output", "stderr": "partial diagnostics", "returncode": -9})


def test_http_evidence_preserves_rejected_response_without_transport_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZNO_NMT_ENDPOINT", "https://private-route.invalid/chat")
    monkeypatch.setenv("ZNO_NMT_KEY", "transport-secret")
    events = []
    response = {"model": "unexpected-model", "choices": []}
    monkeypatch.setattr(adapters, "_http_json", lambda *_args, **_kwargs: response)
    with pytest.raises(adapters.AdapterError, match="model drift"):
        adapters.run_chat_http(
            _packet(), _config(), "closed-book", sources_url=None, prompt="exact prompt",
            evidence=lambda kind, payload: events.append((kind, json.loads(json.dumps(payload)))),
        )
    assert [kind for kind, _ in events] == ["completion_request", "completion_response"]
    assert events[0][1]["messages"] == [{"role": "user", "content": "exact prompt"}]
    assert events[1][1] == response
    assert "transport-secret" not in json.dumps(events)
    assert "private-route.invalid" not in json.dumps(events)


def test_runner_records_prompt_and_failure_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZNO_NMT_ENDPOINT", "https://example.invalid/chat")
    events = []
    exam = {
        "schema": "zno-nmt.exam.v1", "title": "fixture", "subject": "Ukrainian", "year": 2022,
        "provenance": {"source_url": "https://example.invalid", "source_revision": "fixture", "license": "test", "exposure": "public"},
        "scoring": {"kind": "benchmark", "policy_url": None, "pass_threshold": None, "expected_items": 1, "expected_points": 1},
        "items": [{"id": "1", "kind": "single", "question": "Питання", "options": [{"id": "A", "text": "Варіант"}], "rows": [], "correct": "A"}],
    }
    packet, _key = prepare_exam(exam)
    def fail(*_args: Any, **kwargs: Any) -> Any:
        kwargs["evidence"]("completion_response", {"model": "wrong-model"})
        raise adapters.AdapterError("HTTP model drift")
    monkeypatch.setattr(adapters, "run_chat_http", fail)
    result = runner.run_exam(
        packet, _config(), "closed-book",
        evidence=lambda kind, payload: events.append((kind, json.loads(json.dumps(payload)))),
    )
    assert result["status"] == "failed"
    assert [kind for kind, _ in events] == [
        "trial_input", "preflight", "prompt", "completion_response", "trial_failure",
    ]
    assert events[-1][1] == result
    assert events[2][1]["text"] == adapters.build_prompt(packet, "closed-book", max_tool_calls=2)


def test_malformed_completion_bytes_are_preserved_before_json_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64
    from contextlib import closing
    from io import BytesIO

    raw = b"not JSON: \xff"
    class Opener:
        def open(self, *_args: Any, **_kwargs: Any):
            return closing(BytesIO(raw))
    monkeypatch.setattr(adapters.urllib.request, "build_opener", lambda *_args: Opener())
    events = []
    with pytest.raises(adapters.AdapterError, match="response invalid"):
        adapters._http_json(
            "https://example.invalid/chat", {}, key="not-recorded", timeout=1, max_bytes=100,
            evidence=lambda kind, payload: events.append((kind, payload)),
        )
    assert len(events) == 1
    assert events[0][0] == "completion_body"
    assert base64.b64decode(events[0][1]["base64"]) == raw
    assert events[0][1]["truncated"] is False
    assert "not-recorded" not in json.dumps(events)
