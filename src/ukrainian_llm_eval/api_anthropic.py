"""Direct Anthropic Messages API adapter (closed-book), with Message Batches.

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
        # A retried create could submit a second paid request or batch.
        return anthropic.Anthropic(max_retries=0)
    except Exception as exc:  # missing key: the SDK message is not echoed
        raise AdapterError("API authentication unavailable") from exc


def build_request(packet: Mapping[str, Any], config: Mapping[str, Any], prompt: str) -> dict[str, Any]:
    """Messages API parameters for one fresh, single-turn trial."""
    schema = api_suite.api_schema(packet, enum_nulls=True)
    if api_suite.union_parameters(schema) > api_suite.MAX_UNION_PARAMETERS:
        # The API would reject the whole request (or batch line); refuse while planning.
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


def submit_batch(client: Any, requests: list[tuple[str, Mapping[str, Any]]]) -> str:
    try:
        batch = client.messages.batches.create(
            requests=[{"custom_id": custom_id, "params": dict(params)} for custom_id, params in requests]
        )
    except Exception as exc:
        raise AdapterError("API batch submission " + api_suite.sdk_reason(exc)) from exc
    batch_id = api_suite.as_dict(batch).get("id")
    if not isinstance(batch_id, str) or not batch_id:
        raise AdapterError("API batch response is invalid")
    return batch_id


def batch_ended(client: Any, batch_id: str) -> bool:
    try:
        status = api_suite.as_dict(client.messages.batches.retrieve(batch_id)).get("processing_status")
    except Exception as exc:
        raise AdapterError("API batch status " + api_suite.sdk_reason(exc)) from exc
    if status not in {"in_progress", "canceling", "ended"}:
        raise AdapterError("API batch status is invalid")
    return status == "ended"


def _entry_outcome(entry: Any) -> tuple[Any, api_suite.BatchOutcome, Any]:
    """Classify one batch entry; a malformed entry never affects its siblings."""
    if isinstance(entry, str):
        try:
            entry = adapters._strict_json_loads(entry)
        except AdapterError:
            return None, (None, "batch_invalid_result"), {"unparseable_line": entry}
    try:
        value = api_suite.as_dict(entry)
    except (AdapterError, TypeError, ValueError):
        return None, (None, "batch_invalid_result"), {"unreadable_entry": True}
    result = value.get("result")
    kind = result.get("type") if isinstance(result, Mapping) else None
    if not isinstance(kind, str):
        kind = None
    if kind == "succeeded" and isinstance(result.get("message"), Mapping):
        outcome: api_suite.BatchOutcome = (dict(result["message"]), None)
    elif kind in {"errored", "canceled", "expired"}:
        outcome = (None, f"batch_{kind}")
    else:
        outcome = (None, "batch_invalid_result")
    return value.get("custom_id"), outcome, value


def batch_results(client: Any, batch_id: str) -> tuple[dict[str, api_suite.BatchOutcome], list[Any]]:
    """Map each custom id to its message or a normalised failure; return unattributable entries.

    The SDK's JSONL decoder stops at the first malformed line, so the raw
    lines are read from its HTTP response and classified one at a time.
    """
    try:
        decoded = client.messages.batches.results(batch_id)
        response = getattr(decoded, "http_response", None)
        entries = list(response.iter_lines()) if response is not None else list(decoded)
    except Exception as exc:
        raise AdapterError("API batch results " + api_suite.sdk_reason(exc)) from exc
    return api_suite.attribute_batch(
        [_entry_outcome(entry) for entry in entries if not (isinstance(entry, str) and not entry.strip())]
    )


def main(argv: list[str] | None = None) -> int:
    return api_suite.main(sys.modules[__name__], argv)


if __name__ == "__main__":
    raise SystemExit(main())
