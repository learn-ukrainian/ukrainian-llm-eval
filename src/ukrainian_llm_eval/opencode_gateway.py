"""Local OpenCode transport gate; the real provider key never enters the CLI.

Buffer each provider stream until identity, usage and tool policy are checked.
This preserves native OpenCode request construction and tool handling without
allowing its auxiliary agents or retries to escape the evaluator's limits.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

from . import adapters
from .mcp_proxy import Bridge

MAX_BYTES = 2_000_000


def decode_stream(raw: bytes) -> dict[str, Any]:
    """Validate a complete chat-completions stream and assemble its tool calls."""
    models, providers = set(), set()
    calls: dict[int, dict[str, str]] = {}
    content: list[str] = []
    usage = None
    finish = None
    done = False
    for event in raw.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
        if not data:
            continue
        if done:
            raise adapters.AdapterError("OpenCode provider data after stream end")
        if data == "[DONE]":
            done = True
            continue
        body = adapters._strict_json_loads(data)
        if not isinstance(body, dict) or "error" in body:
            raise adapters.AdapterError("OpenCode provider stream error")
        if body.get("model"):
            models.add(body["model"])
        if body.get("provider"):
            providers.add(body["provider"])
        if body.get("usage") is not None:
            if usage is not None and usage != body["usage"]:
                raise adapters.AdapterError("OpenCode provider usage drift")
            usage = body["usage"]
        choices = body.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise adapters.AdapterError("OpenCode provider choices invalid")
        for choice in choices:
            if choice.get("index") != 0:
                raise adapters.AdapterError("OpenCode provider choice index invalid")
            delta = choice.get("delta", {})
            # OpenRouter repeats the terminal reason on its usage-only chunk.
            # Accept that metadata repetition, never more generated content.
            if finish is not None and (
                not isinstance(body.get("usage"), dict) or choice.get("finish_reason") != finish
                or delta.get("content") not in {None, ""} or delta.get("tool_calls")
                or set(delta) - {"content", "role"}
            ):
                raise adapters.AdapterError("OpenCode content after finish")
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            text = delta.get("content")
            if text is not None:
                if not isinstance(text, str):
                    raise adapters.AdapterError("OpenCode provider content invalid")
                content.append(text)
            for item in delta.get("tool_calls", []):
                index = item.get("index")
                if type(index) is not int or index < 0:
                    raise adapters.AdapterError("OpenCode provider tool index invalid")
                call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if item.get("type", "function") != "function":
                    raise adapters.AdapterError("tool_policy_error")
                for field, value in (("id", item.get("id")), ("name", item.get("function", {}).get("name")),
                                     ("arguments", item.get("function", {}).get("arguments"))):
                    if value is not None:
                        if not isinstance(value, str):
                            raise adapters.AdapterError("OpenCode provider tool delta invalid")
                        call[field] += value
    if not done or finish not in {"stop", "tool_calls"} or not isinstance(usage, dict):
        raise adapters.AdapterError("OpenCode provider stream incomplete")
    if bool(calls) != (finish == "tool_calls") or sorted(calls) != list(range(len(calls))):
        raise adapters.AdapterError("OpenCode provider tool stream invalid")
    return {"models": models, "providers": providers, "calls": list(calls.values()),
            "content": "".join(content), "usage": usage, "finish": finish}


class OpenCodeGateway:
    """One-attempt gate for completion transport and filtered reference MCP."""

    def __init__(self, config: Mapping[str, Any], condition: str, *, endpoint: str, key: str,
                 sources_url: str | None, evidence: Callable[[str, Any], None] | None,
                 request_budget: Any, deadline: float):
        self.config = config
        self.condition = condition
        self.endpoint = endpoint
        self.key = key
        self.evidence = evidence
        self.budget = request_budget
        self.deadline = deadline
        self.token = secrets.token_urlsafe(32)
        self.allowed = {"sources_" + name for name in config["tools"]} if condition == "sources" else set()
        self.bridge = Bridge(str(sources_url), list(config["tools"]),
                             max_tool_calls=config["max_tool_calls"]) if condition == "sources" else None
        self.error: Exception | None = None
        self.requests: set[str] = set()
        self.tool_calls = 0
        self.pending_calls: dict[str, list[dict[str, Any]]] = {}
        self.last_content = ""
        self.finished = False
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.cost = Decimal(0)
        self.lock = threading.Lock()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(max(0.1, min(15, gateway.deadline - time.monotonic())))

            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                try:
                    with gateway.lock:
                        if self.headers.get("Authorization") != "Bearer " + gateway.token:
                            self.send_error(403)
                            return
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 0 < size <= MAX_BYTES or gateway.error is not None:
                            raise adapters.AdapterError("OpenCode gateway request rejected")
                        self.connection.settimeout(max(0.1, gateway.deadline - time.monotonic()))
                        body = adapters._strict_json_loads(self.rfile.read(size).decode("utf-8"))
                        if not isinstance(body, dict):
                            raise adapters.AdapterError("OpenCode gateway body invalid")
                        if self.path == "/api/v1/chat/completions":
                            data = gateway.completion(body)
                            content_type = "text/event-stream"
                        elif self.path == "/mcp" and gateway.bridge is not None:
                            reply = gateway.mcp(body)
                            data = adapters.canonical(reply).encode() if reply is not None else b""
                            content_type = "application/json"
                        else:
                            raise adapters.AdapterError("OpenCode gateway path rejected")
                        self.send_response(200 if data else 204)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                except Exception as exc:  # noqa: BLE001 -- retain failures without endpoint/key diagnostics
                    gateway.error = exc
                    try:
                        self.send_error(400, "Evaluator transport rejected request")
                    except OSError:
                        pass

            def do_GET(self) -> None:
                self.send_error(405)

            def do_DELETE(self) -> None:
                self.send_error(405)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A cancelled CLI must not let accounting finalize while an upstream
        # provider response is still being read by a detached handler.
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def record(self, kind: str, value: Any) -> None:
        if self.evidence is not None:
            self.evidence(kind, value)

    def mcp(self, body: dict[str, Any]) -> dict[str, Any] | None:
        assert self.bridge is not None
        if body.get("method") == "tools/call":
            params = body.get("params", {})
            name = "sources_" + str(params.get("name"))
            arguments = params.get("arguments", {})
            pending = self.pending_calls.get(name, [])
            if not pending or arguments != pending[0]:
                raise adapters.AdapterError("tool_policy_error")
            pending.pop(0)
        self.record("opencode_mcp_request", body)
        self.bridge.timeout = max(0.1, min(20, self.deadline - time.monotonic()))
        result = self.bridge.handle(body)
        if body.get("method") == "tools/call":
            if result is None or "error" in result or result.get("result", {}).get("isError"):
                raise adapters.AdapterError("OpenCode reference lookup failed")
            if self.budget is not None:
                self.budget.serialize_tool_result(result["result"])
        self.record("opencode_mcp_response", result)
        return result

    def completion(self, body: dict[str, Any]) -> bytes:
        self.record("completion_request", body)
        if self.error is not None or any(self.pending_calls.values()) or self.finished:
            raise adapters.AdapterError("OpenCode pending tool execution")
        routing = self.config["openrouter"]
        expected_provider = {"only": [routing["provider_endpoint"]], "allow_fallbacks": False,
                             "require_parameters": True}
        if "max_price" in routing:
            expected_provider["max_price"] = dict(routing["max_price"])
        tools = body.get("tools", [])
        if not isinstance(tools, list):
            raise adapters.AdapterError("tool_policy_error")
        names = [tool.get("function", {}).get("name") for tool in tools]
        if len(names) != len(set(names)) or set(names) != self.allowed:
            raise adapters.AdapterError("tool_policy_error")
        if (body.get("model") != self.config["model"] or body.get("max_tokens") != self.config["max_output_tokens"]
                or body.get("provider") != expected_provider
                or body.get("reasoning") != {"enabled": routing["reasoning_enabled"]}
                or body.get("stream") is not True or "response_format" in body
                or body.get("n", 1) != 1 or body.get("tool_choice", "auto") != "auto"):
            raise adapters.AdapterError("OpenCode request configuration drift")
        permitted = {"model", "messages", "max_tokens", "provider", "reasoning", "stream", "stream_options",
                     "tools", "tool_choice", "temperature", "top_p", "parallel_tool_calls", "n", "usage"}
        if set(body) - permitted or not isinstance(body.get("messages"), list):
            raise adapters.AdapterError("OpenCode request fields invalid")
        raw = adapters.canonical(body).encode()
        request_hash = hashlib.sha256(raw).hexdigest()
        if request_hash in self.requests or len(self.requests) >= self.config["max_tool_calls"] + 1:
            raise adapters.AdapterError("OpenCode repeated or excessive request")
        if self.condition == "closed-book" and self.requests:
            raise adapters.AdapterError("OpenCode unexpected auxiliary request")
        self.requests.add(request_hash)
        if self.budget is not None:
            raw, commitment = self.budget.commit_request(body)
            self.record("request_budget_commitment", commitment)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise adapters.AdapterError("OpenCode total timeout")
        request = urllib.request.Request(self.endpoint, data=raw, method="POST",
                                         headers={"Authorization": "Bearer " + self.key,
                                                  "Content-Type": "application/json", "Accept": "text/event-stream"})
        opener = urllib.request.build_opener(adapters._RejectRedirects())
        with opener.open(request, timeout=remaining) as response:  # nosec B310 -- validated operator endpoint
            chunks = []
            received = 0
            while received <= MAX_BYTES:
                if time.monotonic() >= self.deadline:
                    raise adapters.AdapterError("OpenCode total timeout")
                chunk = response.read1(min(65536, MAX_BYTES + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            raw_response = b"".join(chunks)
        if len(raw_response) > MAX_BYTES:
            raise adapters.AdapterError("OpenCode provider response exceeds limit")
        self.record("opencode_provider_stream", {"text": raw_response.decode("utf-8")})
        decoded = decode_stream(raw_response)
        usage = decoded["usage"]
        if self.budget is not None:
            observation = self.budget.observe(usage, tool_calls=len(decoded["calls"]))
            self.record("request_budget_observation", observation)
        if decoded["models"] != {self.config["model"]} or decoded["providers"] != {routing["expected_provider_name"]}:
            raise adapters.AdapterError("OpenCode provider identity drift")
        for target, source in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                               ("total_tokens", "total_tokens")):
            value = usage.get(source)
            if type(value) is not int or value < 0:
                raise adapters.AdapterError("OpenCode provider usage missing")
            self.usage[target] += value
        try:
            charge = Decimal(str(usage["cost"]))
            if not charge.is_finite() or charge < 0:
                raise ValueError
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise adapters.AdapterError("OpenCode provider charge missing") from exc
        self.cost += charge
        self.tool_calls += len(decoded["calls"])
        if self.tool_calls > self.config["max_tool_calls"]:
            raise adapters.AdapterError("tool_limit_error")
        for call in decoded["calls"]:
            if call["name"] not in self.allowed or not call["id"]:
                raise adapters.AdapterError("tool_policy_error")
            arguments = adapters._strict_json_loads(call["arguments"])
            if not isinstance(arguments, dict):
                raise adapters.AdapterError("tool_policy_error")
            self.pending_calls.setdefault(call["name"], []).append(arguments)
        self.last_content = decoded["content"]
        self.finished = decoded["finish"] == "stop"
        return raw_response
