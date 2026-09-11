"""Bounded singleton OpenRouter batch transport for native OpenCode requests.

This bridge changes transport only. Provider controls and native tool schemas
stay on the child request; failure never triggers another submission.
"""
from __future__ import annotations

import copy
import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from . import adapters

MODEL = "google/gemma-4-31b-it:batch"
BASE_MODEL = "google/gemma-4-31b-it"
PROVIDER = "Together"
BATCH_URL = "https://openrouter.ai/api/beta/batches"
GENERATION_URL = "https://openrouter.ai/api/v1/generation"
MAX_BYTES = 2_000_000


def child_request(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("model") != MODEL:
        raise adapters.AdapterError("OpenCode batch model invalid")
    child = copy.deepcopy(body)
    child.update(model=BASE_MODEL, stream=False)
    child.pop("stream_options", None)
    child.pop("usage", None)
    return child


def validate_endpoint(endpoint: str) -> None:
    # Never derive authenticated batch URLs from an arbitrary operator URL.
    parsed = urllib.parse.urlsplit(endpoint)
    if (parsed.scheme != "https" or parsed.netloc != "openrouter.ai"
            or parsed.path not in {"/api/v1/chat/completions", "/api/v1", "/api/v1/"}
            or parsed.query or parsed.fragment):
        raise adapters.AdapterError("OpenCode batch endpoint rejected")


def _identifier(value: Any, prefix: str) -> str:
    if not isinstance(value, str) or re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{1,180}", value) is None:
        raise adapters.AdapterError("OpenCode batch identifier invalid")
    return value


def _usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("is_byok") is not False:
        raise adapters.AdapterError("OpenCode batch BYOK accounting unavailable")
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if type(value.get(name)) is not int or value[name] < 0:
            raise adapters.AdapterError("OpenCode batch usage missing")
    if value["total_tokens"] != value["prompt_tokens"] + value["completion_tokens"]:
        raise adapters.AdapterError("OpenCode batch usage inconsistent")
    try:
        if isinstance(value.get("cost"), bool):
            raise TypeError
        cost = Decimal(str(value["cost"]))
        if not cost.is_finite() or cost < 0:
            raise ValueError
    except (KeyError, InvalidOperation, ValueError, TypeError) as exc:
        raise adapters.AdapterError("OpenCode batch charge missing") from exc
    return value


def _completed(batch: dict[str, Any], custom_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    counts = batch.get("request_counts")
    if (not isinstance(counts, dict) or set(counts) != {"total", "completed", "failed"}
            or any(type(value) is not int for value in counts.values())
            or counts != {"total": 1, "completed": 1, "failed": 0} or batch.get("error") is not None):
        raise adapters.AdapterError("OpenCode batch counts invalid")
    results = batch.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise adapters.AdapterError("OpenCode batch results invalid")
    row = results[0]
    if not isinstance(row, dict) or row.get("custom_id") != custom_id or row.get("error") is not None:
        raise adapters.AdapterError("OpenCode batch correlation invalid")
    response = row.get("response")
    if (not isinstance(response, dict) or type(response.get("status_code")) is not int
            or response["status_code"] != 200 or not isinstance(response.get("body"), dict)):
        raise adapters.AdapterError("OpenCode batch child failed")
    body = response["body"]
    if body.get("model") != BASE_MODEL or body.get("error") is not None:
        raise adapters.AdapterError("OpenCode batch model identity drift")
    return body, _usage(batch.get("usage"))


def _stream(body: dict[str, Any], usage: dict[str, Any]) -> bytes:
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise adapters.AdapterError("OpenCode batch choices invalid")
    choice = choices[0]
    message = choice.get("message")
    if (type(choice.get("index")) is not int or choice["index"] != 0
            or not isinstance(message, dict) or message.get("role") != "assistant"
            or choice.get("finish_reason") not in {"stop", "tool_calls"}):
        raise adapters.AdapterError("OpenCode batch message invalid")
    delta = copy.deepcopy(message)
    calls = delta.get("tool_calls", [])
    if not isinstance(calls, list):
        raise adapters.AdapterError("OpenCode batch tools invalid")
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or ("index" in call and call["index"] != index):
            raise adapters.AdapterError("OpenCode batch tool index invalid")
        call["index"] = index
    if "tool_calls" in delta:
        delta["tool_calls"] = calls
    common = {"id": body["id"], "object": "chat.completion.chunk", "created": body.get("created"),
              "model": body["model"], "provider": body["provider"]}
    events = [{**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
              {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}],
               "usage": usage}]
    return ("".join("data: " + adapters.canonical(event).strip() + "\n\n" for event in events)
            + "data: [DONE]\n\n").encode()


def complete(child_raw: bytes, *, endpoint: str, key: str, deadline: float,
             record: Callable[[str, Any], None]) -> bytes:
    validate_endpoint(endpoint)
    child = adapters._strict_json_loads(child_raw.decode("utf-8"))
    if not isinstance(child, dict) or child.get("model") != BASE_MODEL or child.get("stream") is not False:
        raise adapters.AdapterError("OpenCode batch committed child invalid")
    custom_id = "req-" + hashlib.sha256(child_raw).hexdigest()
    envelope = {"endpoint": "/v1/chat/completions", "model": BASE_MODEL,
                "requests": [{"custom_id": custom_id, "body": child}]}
    raw = adapters.canonical(envelope).encode()
    record("opencode_batch_envelope", {"child_sha256": hashlib.sha256(child_raw).hexdigest(),
                                       "envelope_sha256": hashlib.sha256(raw).hexdigest(), "body": envelope})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), adapters._RejectRedirects())

    def request(url: str, data: bytes | None = None) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise adapters.AdapterError("OpenCode batch total timeout")
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                     headers={"Authorization": "Bearer " + key, "Accept": "application/json",
                                              "Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=remaining) as response:  # nosec B310 -- fixed OpenRouter URLs
                if response.status != (202 if data is not None else 200) or response.geturl() != url:
                    raise adapters.AdapterError("OpenCode batch HTTP response rejected")
                parts, received = [], 0
                while received <= MAX_BYTES:
                    if time.monotonic() >= deadline:
                        raise adapters.AdapterError("OpenCode batch total timeout")
                    part = response.read1(min(65536, MAX_BYTES + 1 - received))
                    if not part:
                        break
                    parts.append(part)
                    received += len(part)
                if received > MAX_BYTES:
                    raise adapters.AdapterError("OpenCode batch response exceeds limit")
                value = adapters._strict_json_loads(b"".join(parts).decode("utf-8"))
                if not isinstance(value, dict):
                    raise adapters.AdapterError("OpenCode batch response invalid")
                return value
        except urllib.error.HTTPError as exc:
            record("opencode_provider_http_error", {"status": exc.code,
                "body": exc.read(8192).decode("utf-8", errors="replace")})
            raise adapters.AdapterError("OpenCode batch HTTP request failed") from exc

    batch = request(BATCH_URL, raw)
    record("opencode_batch_submission", batch)
    batch_id = _identifier(batch.get("id"), "batch_")
    while True:
        if (batch.get("id") != batch_id or batch.get("model") != BASE_MODEL
                or batch.get("endpoint") != "/v1/chat/completions"):
            raise adapters.AdapterError("OpenCode batch identity drift")
        status = batch.get("status")
        if status == "completed":
            break
        if status not in {"validating", "in_progress", "finalizing"}:
            raise adapters.AdapterError("OpenCode batch failed or unavailable")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise adapters.AdapterError("OpenCode batch total timeout")
        time.sleep(min(1, remaining))
        batch = request(BATCH_URL + "/" + batch_id)
        record("opencode_batch_status", batch)
    body, usage = _completed(batch, custom_id)
    generation_id = _identifier(body.get("id"), "gen-")
    if not body.get("provider"):
        generation = request(GENERATION_URL + "?id=" + urllib.parse.quote(generation_id, safe=""))
        record("opencode_batch_generation", generation)
        data = generation.get("data")
        if (not isinstance(data, dict) or data.get("id") != generation_id
                or data.get("model") != BASE_MODEL or data.get("is_byok") is not False):
            raise adapters.AdapterError("OpenCode batch generation identity unavailable")
        body = {**body, "provider": data.get("provider_name")}
    if body.get("provider") != PROVIDER:
        raise adapters.AdapterError("OpenCode batch provider identity drift")
    return _stream(body, usage)
