"""Fail-closed HTTP adapter for the stateless DeepSeek Responses API.

The adapter keeps the evaluator's narrow exam-response contract while mapping
the Responses API's input-item history and function-call protocol.  It never
uses ``previous_response_id`` or a server conversation: every request carries
the complete history from the beginning of this fresh trial.
"""

from __future__ import annotations

import copy
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import adapters
from .gec import GEC_PACKET_SCHEMA

AdapterError = adapters.AdapterError

_RESPONSES_ADAPTER = "responses-http"
_SUPPORTED_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
_OUTPUT_TYPES = frozenset({"reasoning", "message", "function_call"})
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise AdapterError(f"Responses {label} is invalid")
    return value


def _runtime_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Check fields needed at transport time.

    The shared adapter validator owns the complete public configuration
    contract.  This small check keeps the module safe when called directly and
    avoids reading secrets while validating a config object.
    """

    if not isinstance(config, Mapping) or config.get("adapter") != _RESPONSES_ADAPTER:
        raise AdapterError("wrong adapter")
    endpoint_env = config.get("endpoint_env")
    if not isinstance(endpoint_env, str) or _ENV_NAME_RE.fullmatch(endpoint_env) is None:
        raise AdapterError("Responses endpoint environment name is invalid")
    key_env = config.get("key_env")
    if key_env is not None and (
        not isinstance(key_env, str) or _ENV_NAME_RE.fullmatch(key_env) is None
    ):
        raise AdapterError("Responses key environment name is invalid")
    response_format = config.get("http_response_format", "json_schema")
    if not isinstance(response_format, str) or response_format not in {"json_object", "json_schema"}:
        raise AdapterError("Responses response format is unsupported")
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise AdapterError("Responses model is invalid")
    effort = config.get("effort")
    if effort is not None and (not isinstance(effort, str) or effort not in _SUPPORTED_EFFORTS):
        raise AdapterError("Responses effort is unsupported")
    _positive_int(config.get("timeout_seconds"), "timeout")
    _positive_int(config.get("max_output_tokens"), "max output tokens")
    _positive_int(config.get("max_tool_calls"), "tool call limit")
    tools = config.get("tools")
    if (
        not isinstance(tools, list)
        or any(not isinstance(item, str) for item in tools)
        or any(item not in adapters.REFERENCE_TOOLS for item in tools)
    ):
        raise AdapterError("Responses tools are invalid")
    if len(tools) != len(set(tools)):
        raise AdapterError("Responses tools contain duplicates")
    return dict(config)


def _condition_policy(config: Mapping[str, Any], condition: str, sources_url: str | None) -> None:
    try:
        adapters._condition_policy(config, condition, sources_url)
    except (KeyError, TypeError) as exc:
        raise AdapterError("Responses condition configuration is invalid") from exc


def _response_format(packet: Mapping[str, Any], selected: str) -> dict[str, Any]:
    if selected == "json_object":
        return {"type": "json_object"}
    name = "ua_gec_responses" if packet.get("schema") == GEC_PACKET_SCHEMA else "zno_nmt_responses"
    return {"type": "json_schema", "name": name, "schema": adapters.response_schema(packet)}


def _tool_definitions(
    configured: list[str], available: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_name = {str(tool["name"]): tool for tool in available}
    result: list[dict[str, Any]] = []
    for name in configured:
        tool = by_name.get(name)
        if not isinstance(tool, Mapping) or not isinstance(tool.get("inputSchema"), Mapping):
            raise AdapterError("Sources MCP tool schema is unavailable")
        result.append(
            {
                "type": "function",
                "name": adapters._tool_ref(name),
                "description": "Approved Sources reference tool",
                "parameters": copy.deepcopy(dict(tool["inputSchema"])),
            }
        )
    return result


def _request_payload(
    packet: Mapping[str, Any],
    config: Mapping[str, Any],
    condition: str,
    history: list[dict[str, Any]],
    mcp_tools: list[dict[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config["model"],
        "input": copy.deepcopy(history),
        "max_output_tokens": config["max_output_tokens"],
        "stream": False,
        # DeepSeek documents the API as stateless and returns store=false.
        # Sending the explicit false also protects against a compatible proxy
        # that offers server-side storage by default.
        "store": False,
        "text": {
            "format": _response_format(
                packet, config.get("http_response_format", "json_schema")
            )
        },
    }
    if config.get("effort") is not None:
        payload["reasoning"] = {"effort": config["effort"]}
    if condition == "sources":
        payload["tools"] = _tool_definitions(config["tools"], mcp_tools)
        payload["tool_choice"] = "auto"
    else:
        payload["tool_choice"] = "none"
    return payload


def _require_text_parts(item: Mapping[str, Any], *, reasoning: bool) -> None:
    content = item.get("content")
    if not isinstance(content, list) or not content:
        raise AdapterError("Responses output content is invalid")
    expected_type = "reasoning_text" if reasoning else "output_text"
    for part in content:
        if not isinstance(part, Mapping) or part.get("type") != expected_type:
            raise AdapterError("Responses output content part is invalid")
        if not isinstance(part.get("text"), str):
            raise AdapterError("Responses output content text is invalid")


def _output_items(body: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    output = body.get("output")
    if not isinstance(output, list) or not output:
        raise AdapterError("Responses output is invalid")
    calls: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") not in _OUTPUT_TYPES:
            raise AdapterError("Responses output contains an unsupported item")
        if item.get("status") != "completed":
            raise AdapterError("Responses output item is not complete")
        if item.get("type") == "reasoning":
            _require_text_parts(item, reasoning=True)
        elif item.get("type") == "message":
            if item.get("role") != "assistant":
                raise AdapterError("Responses message role is invalid")
            _require_text_parts(item, reasoning=False)
            messages.append(dict(item))
        else:
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not name
                or not isinstance(arguments, str)
            ):
                raise AdapterError("Responses function call is invalid")
            # Validate arguments before the broker receives them.  The exact
            # string remains in the history sent to the provider.
            try:
                parsed = adapters._strict_json_loads(arguments)
            except AdapterError as exc:
                raise AdapterError("Responses function arguments are invalid") from exc
            if not isinstance(parsed, Mapping):
                raise AdapterError("Responses function arguments are invalid")
            calls.append(dict(item))
    if calls and messages:
        raise AdapterError("Responses output mixes function calls and a message")
    if len(messages) > 1:
        raise AdapterError("Responses output contains multiple messages")
    if messages:
        parts = messages[0]["content"]
        text = "".join(str(part["text"]) for part in parts)
    else:
        text = ""
    return calls, messages, text


def _usage(body: Mapping[str, Any], max_output_tokens: int) -> dict[str, int]:
    usage = body.get("usage")
    if not isinstance(usage, Mapping):
        raise AdapterError("Responses usage is missing")
    values: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(name)
        if type(value) is not int or value < 0:
            raise AdapterError("Responses usage is invalid")
        values[name] = value
    # DeepSeek defines output_tokens as visible plus reasoning tokens.  Keep
    # that source-backed meaning explicit at this boundary.
    if values["output_tokens"] > max_output_tokens:
        raise AdapterError("Responses output usage exceeds configured bound")
    if values["total_tokens"] != values["input_tokens"] + values["output_tokens"]:
        raise AdapterError("Responses total usage is inconsistent")
    details = usage.get("output_tokens_details")
    if details is not None:
        if not isinstance(details, Mapping):
            raise AdapterError("Responses output usage details are invalid")
        reasoning = details.get("reasoning_tokens")
        if type(reasoning) is not int or reasoning < 0 or reasoning > values["output_tokens"]:
            raise AdapterError("Responses reasoning usage is invalid")
    input_details = usage.get("input_tokens_details")
    if input_details is not None:
        if not isinstance(input_details, Mapping):
            raise AdapterError("Responses input usage details are invalid")
        cached = input_details.get("cached_tokens")
        if type(cached) is not int or cached < 0 or cached > values["input_tokens"]:
            raise AdapterError("Responses cached usage is invalid")
    return values


def _validate_envelope(
    body: Mapping[str, Any], config: Mapping[str, Any], seen_response_ids: set[str]
) -> tuple[str, dict[str, int]]:
    response_id = body.get("id")
    if not isinstance(response_id, str) or not response_id or response_id in seen_response_ids:
        raise AdapterError("Responses response identity is invalid")
    if body.get("object") != "response":
        raise AdapterError("Responses object type is invalid")
    model = body.get("model")
    if model != config["model"]:
        raise AdapterError("Responses model drift")
    if "store" in body and body["store"] is not False:
        raise AdapterError("Responses storage was enabled")
    if "previous_response_id" in body and body["previous_response_id"] is not None:
        raise AdapterError("Responses server conversation is enabled")
    if "conversation" in body and body["conversation"] is not None:
        raise AdapterError("Responses server conversation is enabled")
    status = body.get("status")
    if status not in {"completed", "incomplete", "failed", "in_progress"}:
        raise AdapterError("Responses final status is invalid")
    usage = _usage(body, config["max_output_tokens"])
    if status != "completed":
        raise AdapterError("Responses final status is not completed")
    return response_id, usage


def _tool_arguments(call: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        value = adapters._strict_json_loads(str(call["arguments"]))
    except AdapterError as exc:
        raise AdapterError("Responses function arguments are invalid") from exc
    if not isinstance(value, Mapping):
        raise AdapterError("Responses function arguments are invalid")
    return value


def run_responses_http(
    packet: Mapping[str, Any],
    config: Mapping[str, Any],
    condition: str,
    *,
    sources_url: str | None,
    prompt: str,
    evidence: Callable[[str, Any], None] | None = None,
    request_budget: Any = None,
) -> dict[str, Any]:
    """Run one fresh DeepSeek Responses API trial.

    The endpoint is selected by an environment-variable name in ``config``;
    its value and bearer token never enter the returned trial identity.  A
    failed or incomplete response is raised without retrying or falling back.
    """

    checked = _runtime_config(config)
    _condition_policy(checked, condition, sources_url)
    endpoint = os.environ.get(str(checked["endpoint_env"]), "")
    key = os.environ.get(str(checked["key_env"]), "") if checked.get("key_env") else None
    if not endpoint:
        raise AdapterError("Responses HTTP endpoint unavailable")

    started = time.monotonic()
    deadline = started + checked["timeout_seconds"]
    mcp_tools: list[dict[str, Any]] = []
    mcp_identity: str | None = None
    if condition == "sources":
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterError("Responses total timeout")
        mcp_tools, mcp_identity = adapters._mcp_list_tools(
            str(sources_url), max(1, int(remaining))
        )
        available = {str(tool.get("name")): tool for tool in mcp_tools}
        if not set(checked["tools"]).issubset(available):
            raise AdapterError("Sources MCP does not expose configured tools")

    history: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    seen_response_ids: set[str] = set()
    retrieval_digests: list[str] = []
    seen_call_ids: set[str] = set()
    total_input = 0
    total_output = 0
    total_tokens = 0
    tool_calls = 0
    max_bytes = min(2_000_000, max(65_536, checked["max_output_tokens"] * 64))
    response_ids: list[str] = []

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterError("Responses total timeout")
        request_base = _request_payload(packet, checked, condition, history, mcp_tools)
        if evidence is not None:
            evidence("completion_request", request_base)
        request_payload: Mapping[str, Any] | bytes = request_base
        if request_budget is not None:
            commit = getattr(request_budget, "commit_request", None)
            if not callable(commit):
                raise AdapterError("Responses request-budget interface unavailable")
            request_payload, prepared = commit(request_base)
            if evidence is not None:
                evidence("request_budget_commitment", prepared)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterError("Responses total timeout")
        body = adapters._http_json(
            endpoint,
            request_payload,
            key=key,
            timeout=remaining,
            max_bytes=max_bytes,
            evidence=evidence,
        )
        if evidence is not None:
            evidence("completion_response", body)
        response_id, usage = _validate_envelope(body, checked, seen_response_ids)
        seen_response_ids.add(response_id)
        response_ids.append(response_id)
        total_input += usage["input_tokens"]
        total_output += usage["output_tokens"]
        total_tokens += usage["total_tokens"]
        calls, messages, message_text = _output_items(body)
        if request_budget is not None:
            observation = request_budget.observe(body["usage"], tool_calls=len(calls))
            if evidence is not None:
                evidence("request_budget_observation", observation)
        if not calls:
            if not messages:
                raise AdapterError("Responses completed without a message")
            try:
                responses = adapters._extract_responses(
                    adapters._strict_json_loads(message_text), packet
                )
            except AdapterError as exc:
                if packet.get("schema") == GEC_PACKET_SCHEMA:
                    raise
                raise AdapterError("Responses output is not JSON") from exc
            break

        if condition != "sources":
            raise AdapterError("tool_policy_error")
        if tool_calls + len(calls) > checked["max_tool_calls"]:
            raise AdapterError("tool_limit_error")
        call_ids = [call["call_id"] for call in calls]
        if len(call_ids) != len(set(call_ids)) or seen_call_ids.intersection(call_ids):
            raise AdapterError("Responses function call identity is duplicated")
        seen_call_ids.update(call_ids)
        # The entire provider output, including reasoning and function-call
        # records, is required input history for the next stateless request.
        history.extend(copy.deepcopy(dict(item)) for item in body["output"])
        for call in calls:
            tool_calls += 1
            name = call["name"]
            allowed_name = adapters._tool_ref(call_name := name.removeprefix("mcp__sources__"))
            if (
                name != allowed_name
                or call_name not in checked["tools"]
                or not name.startswith("mcp__sources__")
            ):
                raise AdapterError("tool_policy_error")
            arguments = _tool_arguments(call)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError("Responses total timeout")
            if evidence is not None:
                evidence(
                    "tool_request",
                    {"name": name, "arguments": dict(arguments), "call_id": call["call_id"]},
                )
            result = adapters._mcp_call(
                str(sources_url), name, arguments, max(1, int(remaining))
            )
            if request_budget is not None:
                serialized = request_budget.serialize_tool_result(result)
            else:
                serialized = adapters.canonical(result)
            if evidence is not None:
                evidence("tool_result", {"call_id": call["call_id"], "result": result})
            retrieval_digests.append(adapters.digest(result))
            history.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": serialized,
                }
            )

    assert response_ids
    return {
        "responses": responses,
        "identity": {
            "adapter": _RESPONSES_ADAPTER,
            "harness": "deepseek-responses-http",
            "model": checked["model"],
            "provider": checked.get("provider") or "deepseek-responses",
            "session_id": response_ids[0],
            "requested_model": checked["model"],
            "effective_model": checked["model"],
            "requested_effort": checked.get("effort"),
            "effective_effort": "unknown",
            "cli_version": None,
            "tool_schema_sha256": adapters.digest(mcp_tools),
            "mcp_server_identity_sha256": mcp_identity,
            "corpus_id_sha256": (
                adapters.digest(checked["corpus_id"])
                if checked.get("corpus_id") is not None
                else None
            ),
            "retrieval_receipt_sha256": (
                adapters.digest(retrieval_digests) if retrieval_digests else None
            ),
            "response_ids_sha256": adapters.digest(response_ids),
            "store_requested": False,
            "previous_response_id_used": False,
        },
        "metrics": {
            "elapsed_seconds": time.monotonic() - started,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_tokens,
            # DeepSeek Responses usage does not document a per-response cost
            # field. Cost remains unknown and is settled by the budget route.
            "cost_usd": None,
            "tool_calls": tool_calls,
        },
    }


__all__ = ["run_responses_http"]
