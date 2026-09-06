"""Native OpenCode controls using fake provider streams, never paid inference."""

import copy
import http.client
import io
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from ukrainian_llm_eval import adapters
from ukrainian_llm_eval import native_opencode as native
from ukrainian_llm_eval.opencode_gateway import OpenCodeGateway, decode_stream


def config(reasoning=False):
    return {"schema": "zno-nmt.config.v1", "adapter": "opencode", "model": "google/gemma-4-31b-it",
            "effort": None, "timeout_seconds": 15, "max_output_tokens": 4096, "max_tool_calls": 1,
            "repeats": 1, "tools": ["verify_word"], "corpus_id": "fixture", "provider": "openrouter",
            "endpoint_env": "EVAL_TEST_ENDPOINT", "key_env": "EVAL_TEST_KEY", "opencode_bin": "fixture",
            "openrouter": {"provider_endpoint": "venice/bf16", "expected_provider_name": "Venice",
                           "reasoning_enabled": reasoning}}


def packet():
    return {"schema": "zno-nmt.questions.v1", "packet_sha256": "a" * 64,
            "items": [{"id": "q1", "kind": "single", "question": "Fixture", "options": [{"id": "A", "text": "A"}], "rows": []}]}


def payload(gateway):
    body = {"model": gateway.config["model"], "max_tokens": 4096, "stream": True,
            "messages": [{"role": "user", "content": "fixture"}],
            "reasoning": {"enabled": gateway.config["openrouter"]["reasoning_enabled"]},
            "provider": {"only": ["venice/bf16"], "allow_fallbacks": False, "require_parameters": True}}
    if gateway.allowed:
        body["tools"] = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
                         for name in sorted(gateway.allowed)]
    return body


def stream(*, call=None, text='{"responses":{"q1":"A"}}', model="google/gemma-4-31b-it", provider="Venice"):
    delta = {"content": text}
    if call:
        delta = {"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                                 "function": {"name": call, "arguments": '{"word":"fixture"}'}}]}
    common = {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": model, "provider": provider}
    chunks = [{**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
              {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if call else "stop"}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.00001}}]
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


def gateway(condition="closed-book", reasoning=False, **kwargs):
    return OpenCodeGateway(config(reasoning), condition, endpoint="https://provider.invalid/chat", key="parent-secret",
                           sources_url="http://reference.invalid/mcp" if condition == "sources" else None,
                           evidence=kwargs.get("evidence"), request_budget=kwargs.get("budget"), deadline=time.monotonic() + 15)


def provider(monkeypatch, responses):
    seen = []
    def send(request, **_kwargs):
        seen.append(request)
        return io.BytesIO(responses.pop(0))
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: SimpleNamespace(open=send))
    return seen


@pytest.mark.parametrize("reasoning", [False, True])
@pytest.mark.parametrize("condition", ["closed-book", "sources"])
def test_native_config_isolates_route_and_tools(condition, reasoning):
    with gateway(condition, reasoning) as g:
        value = native.native_config(config(reasoning), condition, g)
        assert value["enabled_providers"] == ["openrouter"]
        assert value["agent"]["title"]["disable"] and value["agent"]["summary"]["disable"]
        assert value["permission"] == {"*": "deny", **({"sources_verify_word": "allow"} if condition == "sources" else {})}
        assert "parent-secret" not in json.dumps(value)
        assert ("mcp" in value) == (condition == "sources")


@pytest.mark.parametrize("change", [
    {"model": "other"}, {"max_tokens": 65536}, {"reasoning": {"enabled": True}},
    {"provider": {"order": ["Venice"]}}, {"response_format": {"type": "json_object"}},
    {"tools": [{"type": "function", "function": {"name": "bash"}}]}, {"n": 2}, {"extra_body": {}},
])
def test_bad_requests_never_reach_provider(monkeypatch, change):
    seen = provider(monkeypatch, [])
    with gateway() as g:
        body = payload(g)
        body.update(change)
        with pytest.raises(adapters.AdapterError):
            g.completion(body)
    assert seen == []


def test_response_identity_drift_is_not_released(monkeypatch):
    provider(monkeypatch, [stream(provider="Other")])
    with gateway() as g, pytest.raises(adapters.AdapterError, match="identity drift"):
        g.completion(payload(g))


def test_forbidden_provider_tool_is_not_released(monkeypatch):
    provider(monkeypatch, [stream(call="bash")])
    with gateway() as g, pytest.raises(adapters.AdapterError, match="tool_policy"):
        g.completion(payload(g))


def test_duplicate_request_cannot_spend_again(monkeypatch):
    seen = provider(monkeypatch, [stream()])
    with gateway() as g:
        g.completion(payload(g))
        with pytest.raises(adapters.AdapterError):
            g.completion(payload(g))
    assert len(seen) == 1


def test_unfinished_stream_rejected():
    with pytest.raises(adapters.AdapterError, match="incomplete"):
        decode_stream(stream().replace(b"data: [DONE]\n\n", b""))


def test_tool_call_must_match_provider_and_cap(monkeypatch):
    seen = provider(monkeypatch, [stream(call="sources_verify_word"), stream(call="sources_verify_word")])
    with gateway("sources") as g:
        body = payload(g)
        g.completion(body)
        g.bridge.handle = lambda message: {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "fixture"}]}}
        with pytest.raises(adapters.AdapterError, match="tool_policy"):
            g.mcp({"id": 1, "method": "tools/call", "params": {"name": "verify_word", "arguments": {"word": "other"}}})
        g.mcp({"id": 2, "method": "tools/call", "params": {"name": "verify_word", "arguments": {"word": "fixture"}}})
        body["messages"].append({"role": "tool", "content": "fixture", "tool_call_id": "call-1"})
        with pytest.raises(adapters.AdapterError, match="tool_limit"):
            g.completion(body)
    assert len(seen) == 2


def test_budget_commits_before_transport_and_observes_before_release(monkeypatch):
    events = []
    class Budget:
        def commit_request(self, body):
            events.append("commit")
            return json.dumps(body).encode(), {}
        def observe(self, usage, *, tool_calls):
            events.append("observe")
            assert usage["cost"] == 0.00001 and tool_calls == 0
            return {}
    def send(request, **_kwargs):
        assert events == ["commit"]
        events.append("transport")
        return io.BytesIO(stream())
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: SimpleNamespace(open=send))
    with gateway(budget=Budget()) as g:
        g.completion(payload(g))
        assert events == ["commit", "transport", "observe"]


def test_child_environment_cannot_inherit_credentials_or_customizations(monkeypatch, tmp_path):
    for name in ("OPENROUTER_API_KEY", "EVAL_TEST_KEY", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR", "PYTHONPATH"):
        monkeypatch.setenv(name, "must-not-inherit")
    env = native.child_env(tmp_path, tmp_path / "config.json")
    assert "must-not-inherit" not in env.values()
    assert env["HOME"] == str(tmp_path)
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"


def events(text='{"responses":{"q1":"A"}}'):
    return "\n".join(json.dumps(item) for item in [
        {"type": "text", "sessionID": "session", "part": {"messageID": "message", "text": text}},
        {"type": "step_finish", "sessionID": "session", "part": {"messageID": "message", "reason": "stop"}},
    ])


@pytest.mark.parametrize("text", ['```json\n{"responses":{"q1":"A"}}\n```', '{"responses":{}}'])
def test_final_answers_are_not_repaired(text):
    with pytest.raises(adapters.AdapterError):
        native.parse_events(events(text), packet(), set())


def test_live_without_budget_stops_before_binary(monkeypatch):
    monkeypatch.setenv("EVAL_TEST_ENDPOINT", "https://openrouter.ai/api/v1/chat/completions")
    monkeypatch.setenv("EVAL_TEST_KEY", "fixture")
    monkeypatch.setattr(native, "_binary", lambda _config: pytest.fail("binary must not launch"))
    with pytest.raises(adapters.AdapterError, match="budget"):
        native.run_opencode(packet(), config(), "closed-book", sources_url=None, prompt="fixture")


def test_runner_preserves_native_route_and_parent_key(monkeypatch):
    monkeypatch.setenv("EVAL_TEST_ENDPOINT", "http://localhost:12345/completion-fixture")
    monkeypatch.setenv("EVAL_TEST_KEY", "parent-secret")
    monkeypatch.setattr(native, "_binary", lambda _config: ("fixture", "1.0", "a" * 64))
    seen = provider(monkeypatch, [stream()])
    def child(argv, *, cwd, env, prompt, **_kwargs):
        assert prompt == "fixture" and not list(cwd.iterdir())
        assert "parent-secret" not in env.values()
        settings = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
        options = settings["provider"]["openrouter"]["options"]
        url = urlsplit(options["baseURL"])
        connection = http.client.HTTPConnection(url.hostname, url.port)
        body = {"model": "google/gemma-4-31b-it", "max_tokens": 4096, "stream": True,
                "messages": [{"role": "user", "content": prompt}], "reasoning": {"enabled": False},
                "provider": {"only": ["venice/bf16"], "allow_fallbacks": False, "require_parameters": True}}
        connection.request("POST", "/api/v1/chat/completions", body=json.dumps(body),
                           headers={"Authorization": "Bearer " + options["apiKey"], "Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        connection.close()
        return subprocess.CompletedProcess(argv, 0, stdout=events(), stderr="")
    monkeypatch.setattr(adapters, "_run_claude_process", child)
    result = native.run_opencode(packet(), config(), "closed-book", sources_url=None, prompt="fixture")
    assert result["responses"] == {"q1": "A"}
    assert result["identity"]["harness"] == "opencode-cli"
    assert seen[0].get_header("Authorization") == "Bearer parent-secret"
    assert result["metrics"]["cost_usd"] == 0.00001


def test_reasoning_changes_pair_identity():
    from ukrainian_llm_eval.runner import _comparison
    first = config()
    second = copy.deepcopy(first)
    second["openrouter"]["reasoning_enabled"] = True
    assert _comparison(packet(), first) != _comparison(packet(), second)
