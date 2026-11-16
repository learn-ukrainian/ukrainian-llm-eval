"""Direct OpenAI Responses API adapter (closed-book), with the Batch API.

Run ``python -m ukrainian_llm_eval.api_openai --help``.  The official
``openai`` SDK reads ``OPENAI_API_KEY`` itself; this module never reads or
stores the key.  Requests use strict structured output for the shared
response schema, a fixed ``max_output_tokens`` (which also bounds reasoning),
the configured reasoning effort and, only when configured, temperature.
Responses are not stored server side (``store: false``).
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

from . import adapters, api_suite

ADAPTER = "openai-api"
AdapterError = adapters.AdapterError
_ENDPOINT = "/v1/responses"
_BATCH_ENDED = frozenset({"completed", "failed", "expired", "cancelled"})
_BATCH_RUNNING = frozenset({"validating", "in_progress", "finalizing", "cancelling"})


def make_client() -> Any:
    """Create an SDK client without retries; the SDK reads the key itself."""
    try:
        import openai
    except ImportError as exc:
        raise AdapterError("API SDK unavailable; install ukrainian-llm-eval[api]") from exc
    try:
        # A retried create could submit a second paid request or batch.
        return openai.OpenAI(max_retries=0)
    except Exception as exc:  # missing key: the SDK message is not echoed
        raise AdapterError("API authentication unavailable") from exc


def build_request(packet: Mapping[str, Any], config: Mapping[str, Any], prompt: str) -> dict[str, Any]:
    """Responses API body for one fresh, single-turn, unstored trial."""
    params: dict[str, Any] = {
        "model": config["model"],
        "input": [{"role": "user", "content": prompt}],
        "max_output_tokens": config["max_output_tokens"],
        "text": {"format": {"type": "json_schema", "name": api_suite.response_name(packet),
                            "schema": api_suite.api_schema(packet), "strict": True}},
        "store": False,
    }
    if config["effort"] is not None:
        params["reasoning"] = {"effort": config["effort"]}
    if config.get("temperature") is not None:
        params["temperature"] = config["temperature"]
    return params


def call(client: Any, params: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    return api_suite.as_dict(client.responses.create(**params, timeout=timeout))


def read_usage(raw: Mapping[str, Any]) -> dict[str, int]:
    usage = raw.get("usage")
    if not isinstance(usage, Mapping):
        raise AdapterError("API usage is missing")
    result = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AdapterError("API usage is invalid")
        result[field] = value
    return result


def read_answers(raw: Mapping[str, Any], packet: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the response envelope and return checked answers."""
    if raw.get("object") != "response":
        raise AdapterError("API response envelope is invalid")
    model = api_suite.check_model(config, raw.get("model"))
    status = raw.get("status")
    if status == "incomplete":
        details = raw.get("incomplete_details")
        if isinstance(details, Mapping) and details.get("reason") == "max_output_tokens":
            raise AdapterError("API output limit reached")
        raise AdapterError("API response incomplete")
    if status != "completed":
        raise AdapterError("API response status is not completed")
    output = raw.get("output")
    if not isinstance(output, list):
        raise AdapterError("API response output is invalid")
    texts = []
    for item in output:
        kind = item.get("type") if isinstance(item, Mapping) else None
        kind = kind if isinstance(kind, str) else None
        if kind == "reasoning":
            continue
        if kind != "message" or item.get("role") != "assistant" or not isinstance(item.get("content"), list):
            # A function or tool call is outside the closed-book contract.
            raise AdapterError("tool_policy_error" if kind and "call" in kind else "API response output is invalid")
        for part in item["content"]:
            part_type = part.get("type") if isinstance(part, Mapping) else None
            if part_type == "refusal":
                raise AdapterError("API response refusal")
            if part_type != "output_text" or not isinstance(part.get("text"), str):
                raise AdapterError("API response output is invalid")
            texts.append(part["text"])
    if not texts:
        raise AdapterError("API response output is missing")
    try:
        responses = adapters._extract_responses(adapters._strict_json_loads("".join(texts)), packet)
    except AdapterError as exc:
        raise AdapterError("API response schema mismatch") from exc
    response_id = raw.get("id")
    return {"responses": responses, "model": model, "id": response_id if isinstance(response_id, str) else None}


def submit_batch(client: Any, requests: list[tuple[str, Mapping[str, Any]]]) -> str:
    lines = [
        adapters.canonical({"custom_id": custom_id, "method": "POST", "url": _ENDPOINT, "body": dict(params)})
        for custom_id, params in requests
    ]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        uploaded = api_suite.as_dict(client.files.create(file=("ukrainian-llm-eval-batch.jsonl", data), purpose="batch"))
        batch = api_suite.as_dict(client.batches.create(
            input_file_id=uploaded["id"], endpoint=_ENDPOINT, completion_window="24h"))
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError("API batch submission " + api_suite.sdk_reason(exc)) from exc
    batch_id = batch.get("id")
    if not isinstance(batch_id, str) or not batch_id:
        raise AdapterError("API batch response is invalid")
    return batch_id


def _retrieve(client: Any, batch_id: str) -> dict[str, Any]:
    try:
        batch = api_suite.as_dict(client.batches.retrieve(batch_id))
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError("API batch status " + api_suite.sdk_reason(exc)) from exc
    if batch.get("status") not in _BATCH_ENDED | _BATCH_RUNNING:
        raise AdapterError("API batch status is invalid")
    return batch


def batch_ended(client: Any, batch_id: str) -> bool:
    return _retrieve(client, batch_id)["status"] in _BATCH_ENDED


def _file_text(client: Any, file_id: Any) -> str:
    if file_id is None:
        return ""
    try:
        content = client.files.content(file_id)
        return content.text if isinstance(getattr(content, "text", None), str) else content.read().decode("utf-8")
    except Exception as exc:
        raise AdapterError("API batch results " + api_suite.sdk_reason(exc)) from exc


def _line_outcome(line: str) -> tuple[Any, api_suite.BatchOutcome, Any]:
    try:
        row = adapters._strict_json_loads(line)
    except AdapterError:
        return None, (None, "batch_invalid_result"), {"unparseable_line": line}
    if not isinstance(row, dict):
        return None, (None, "batch_invalid_result"), {"unparseable_line": line}
    response = row.get("response")
    status = response.get("status_code") if isinstance(response, Mapping) else None
    if status == 200 and row.get("error") is None and isinstance(response.get("body"), Mapping):
        outcome: api_suite.BatchOutcome = (dict(response["body"]), None)
    elif isinstance(status, int) and not isinstance(status, bool):
        outcome = (None, f"http_status_{status}")
    else:
        outcome = (None, "batch_errored")
    return row.get("custom_id"), outcome, row


def batch_results(client: Any, batch_id: str) -> tuple[dict[str, api_suite.BatchOutcome], list[Any]]:
    """Map each custom id to its response body or a normalised failure; return unattributable lines."""
    batch = _retrieve(client, batch_id)
    text = _file_text(client, batch.get("output_file_id")) + "\n" + _file_text(client, batch.get("error_file_id"))
    return api_suite.attribute_batch([_line_outcome(line) for line in text.splitlines() if line.strip()])


def main(argv: list[str] | None = None) -> int:
    return api_suite.main(sys.modules[__name__], argv)


if __name__ == "__main__":
    raise SystemExit(main())
