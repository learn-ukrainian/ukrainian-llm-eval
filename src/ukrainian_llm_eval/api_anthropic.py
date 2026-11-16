"""Direct Anthropic Messages API adapter (closed-book, synchronous).

Run ``python -m ukrainian_llm_eval.api_anthropic --help``.  The official
``anthropic`` SDK reads ``ANTHROPIC_API_KEY`` itself; this module never reads
or stores the key.  Requests use structured output for the shared response
schema, a fixed ``max_tokens`` (which also bounds adaptive thinking), and the
configured effort and temperature.  Claude Opus 5.5 rejects ``temperature``,
so configurations for it must leave temperature null; the recorded value is
then null, meaning the provider's fixed default.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

from . import adapters, api_suite

ADAPTER = "anthropic-api"
AdapterError = adapters.AdapterError
_TEXT_FREE_BLOCKS = frozenset({"thinking", "redacted_thinking"})


def make_client() -> Any:
    """Create an SDK client without retries; the SDK reads the key itself."""
    try:
        import anthropic
    except ImportError as exc:
        raise AdapterError("API SDK unavailable; install ukrainian-llm-eval[api]") from exc
    try:
        # A retried create could submit a second paid request.
        return anthropic.Anthropic(max_retries=0)
    except Exception as exc:  # missing key: the SDK message is not echoed
        raise AdapterError("API authentication unavailable") from exc


def build_request(packet: Mapping[str, Any], config: Mapping[str, Any], prompt: str) -> dict[str, Any]:
    """Messages API parameters for one fresh, single-turn trial."""
    schema = api_suite.api_schema(packet, enum_nulls=True)
    if api_suite.union_parameters(schema) > api_suite.MAX_UNION_PARAMETERS:
        # The API would reject the request; refuse while planning, before any paid call.
        raise AdapterError("API schema exceeds 16 union-typed answers; use a segment plan")
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    if config["effort"] is not None:
        output_config["effort"] = config["effort"]
    params: dict[str, Any] = {
        "model": config["model"],
        "max_tokens": config["max_output_tokens"],
        "messages": [{"role": "user", "content": prompt}],
        "output_config": output_config,
    }
    if config.get("temperature") is not None:
        params["temperature"] = config["temperature"]
    return params


def call(client: Any, params: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    return api_suite.as_dict(client.messages.create(**params, timeout=timeout))


def _count(usage: Mapping[str, Any], field: str, *, required: bool) -> int:
    value = usage.get(field)
    if value is None and not required:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterError("API usage is invalid")
    return value


def read_usage(raw: Mapping[str, Any]) -> dict[str, int]:
    usage = raw.get("usage")
    if not isinstance(usage, Mapping):
        raise AdapterError("API usage is missing")
    result = {field: _count(usage, field, required=True) for field in ("input_tokens", "output_tokens")}
    for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        result[field] = _count(usage, field, required=False)
    result["total_tokens"] = sum(result.values())
    return result


def read_answers(raw: Mapping[str, Any], packet: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the message envelope and return checked answers."""
    if raw.get("type") != "message" or raw.get("role") != "assistant":
        raise AdapterError("API response envelope is invalid")
    model = api_suite.check_model(config, raw.get("model"))
    stop = raw.get("stop_reason")
    if stop == "max_tokens":
        raise AdapterError("API output limit reached")
    if stop == "refusal":
        raise AdapterError("API response refusal")
    if stop != "end_turn":
        raise AdapterError("API response stop reason is unsupported")
    content = raw.get("content")
    if not isinstance(content, list):
        raise AdapterError("API response content is invalid")
    texts = []
    for block in content:
        kind = block.get("type") if isinstance(block, Mapping) else None
        if isinstance(kind, str) and kind in _TEXT_FREE_BLOCKS:
            continue
        if kind != "text" or not isinstance(block.get("text"), str):
            raise AdapterError("API response content is invalid")
        texts.append(block["text"])
    if not texts:
        raise AdapterError("API response output is missing")
    try:
        responses = adapters._extract_responses(adapters._strict_json_loads("".join(texts)), packet)
    except AdapterError as exc:
        raise AdapterError("API response schema mismatch") from exc
    message_id = raw.get("id")
    return {"responses": responses, "model": model, "id": message_id if isinstance(message_id, str) else None}


def main(argv: list[str] | None = None) -> int:
    return api_suite.main(sys.modules[__name__], argv)


if __name__ == "__main__":
    raise SystemExit(main())
