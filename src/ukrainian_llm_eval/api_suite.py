"""Shared support for the direct, paid provider API adapters.

``api_anthropic`` (Messages API) and ``api_openai`` (Responses API) only
produce answers.  This module holds what they share: configuration checks,
list prices and cost estimates, the runner-path trial, and one suite command
that runs a packet (optionally split by a frozen segment plan) either
synchronously or as a provider batch, then writes the existing run and
evidence formats so ``score`` and ``score-gec`` work unchanged.

The adapters are closed-book only.  Keys are read by the official SDKs from
their standard environment variables; this package never reads, prints or
stores a key value.  Every paid suite run needs ``--max-usd`` and stops before
any request when its conservative estimate is higher.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any

from . import adapters, gec
from .core import ExamError, digest, read_json, validate_packet, write_private_json
from .evidence import EvidenceStore

AdapterError = adapters.AdapterError

# USD per million tokens (input, output) at standard list price, checked
# 2026-11-16: platform.claude.com/docs/en/about-claude/pricing and
# developers.openai.com/api/docs/models/gpt-6.1-sol.  Both Batch APIs bill 50%.
LIST_PRICES_USD_PER_MTOK: dict[str, tuple[str, str]] = {
    "claude-opus-5-5": ("4", "20"),
    "claude-sonnet-5-5": ("2", "10"),
    "gpt-6.1-sol": ("2", "10"),
}
PRICES_CHECKED = "2026-11-16"
BATCH_PRICE_FACTOR = Decimal("0.5")
_MILLION = Decimal(1_000_000)
_PROVIDERS = {
    "anthropic-api": {
        "efforts": frozenset({"low", "medium", "high", "xhigh", "max"}),
        "temperature_max": 1,
        "harness": "anthropic-messages",
        "batch": "anthropic-message-batches",
    },
    "openai-api": {
        "efforts": frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"}),
        "temperature_max": 2,
        "harness": "openai-responses",
        "batch": "openai-batch",
    },
}
_FIELDS = {
    "schema", "adapter", "model", "effort", "temperature", "timeout_seconds", "max_output_tokens",
    "max_tool_calls", "repeats", "tools", "corpus_id", "provider", "max_usd_per_trial", "usd_per_mtok",
}
_REQUIRED = _FIELDS - {"temperature", "provider", "corpus_id", "max_usd_per_trial", "usd_per_mtok"}
ROW_SCHEMA = "ukrainian-llm-eval.api-row.v1"
SUITE_SCHEMA = "ukrainian-llm-eval.api-suite.v1"


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise AdapterError(f"configuration {label} must be a nonnegative number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise AdapterError(f"configuration {label} must be a nonnegative number") from exc
    if not number.is_finite() or number < 0:
        raise AdapterError(f"configuration {label} must be a nonnegative number")
    return number


def validate_api_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Check a serialisable direct-API configuration without reading secrets."""
    if not isinstance(config, Mapping) or config.get("adapter") not in _PROVIDERS:
        raise AdapterError("configuration adapter is unsupported")
    spec = _PROVIDERS[config["adapter"]]
    if set(config) - _FIELDS or _REQUIRED - set(config):
        raise AdapterError("configuration contains unsupported or missing fields")
    if config["schema"] != "zno-nmt.config.v1":
        raise AdapterError("configuration schema mismatch")
    model = config["model"]
    if not isinstance(model, str) or not model or model.strip() != model:
        raise AdapterError("configuration model must be a nonempty string")
    effort = config["effort"]
    if effort is not None and effort not in spec["efforts"]:
        raise AdapterError("configuration effort is unsupported by this API")
    temperature = config.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool) or not isinstance(temperature, (int, float))
        or not 0 <= temperature <= spec["temperature_max"]
    ):
        raise AdapterError("configuration temperature is out of range")
    for field in ("timeout_seconds", "max_output_tokens", "max_tool_calls", "repeats"):
        value = config[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AdapterError(f"configuration {field} must be positive")
    if config["tools"] != []:
        raise AdapterError("direct API adapters are closed-book only; tools must be empty")
    for field in ("corpus_id", "provider"):
        value = config.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise AdapterError(f"configuration {field} must be string or null")
    if config.get("max_usd_per_trial") is not None:
        _decimal(config["max_usd_per_trial"], "max_usd_per_trial")
    if "usd_per_mtok" in config:
        prices = config["usd_per_mtok"]
        if not isinstance(prices, Mapping) or set(prices) != {"input", "output"}:
            raise AdapterError("configuration usd_per_mtok needs input and output")
        _decimal(prices["input"], "usd_per_mtok")
        _decimal(prices["output"], "usd_per_mtok")
    checked = dict(config)
    checked.setdefault("temperature", None)
    checked.setdefault("provider", None)
    checked.setdefault("corpus_id", None)
    return checked


def preflight(config: Mapping[str, Any], condition: str) -> dict[str, Any]:
    """Offline capability record; no endpoint, key or network access."""
    checked = validate_api_config(config)
    if condition != "closed-book":
        raise AdapterError("direct API capability is closed-book only")
    return {
        "schema": "zno-nmt.capability.v1",
        "adapter": checked["adapter"],
        "condition": condition,
        "requested_model": checked["model"],
        "requested_effort": checked["effort"],
        "tools_sha256": digest(checked["tools"]),
        "corpus_id_sha256": digest(checked["corpus_id"]) if checked["corpus_id"] is not None else None,
        "capability": checked["adapter"] + "-direct",
        "cli_version": None,
        "tool_schema_sha256": digest([]),
        "mcp_server_identity_sha256": None,
    }


def prices(config: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    """Return list prices per million tokens; unknown models need ``usd_per_mtok``."""
    if "usd_per_mtok" in config:
        override = config["usd_per_mtok"]
        return _decimal(override["input"], "usd_per_mtok"), _decimal(override["output"], "usd_per_mtok")
    if config["model"] not in LIST_PRICES_USD_PER_MTOK:
        raise AdapterError("model price is unknown; set usd_per_mtok in the configuration")
    raw_in, raw_out = LIST_PRICES_USD_PER_MTOK[config["model"]]
    return Decimal(raw_in), Decimal(raw_out)


MAX_UNION_PARAMETERS = 16


def api_schema(packet: Mapping[str, Any], *, enum_nulls: bool = False) -> dict[str, Any]:
    """The shared response schema, narrowed to what structured outputs accept.

    Neither provider accepts string length keywords; the GEC line pattern
    already requires one character.  A GEC answer must be a correction (the
    prompt asks for the unchanged sentence when nothing is wrong), so its
    ``null`` branch is dropped.  With ``enum_nulls`` (Anthropic, which allows
    at most 16 union-typed parameters per request) a nullable option enum
    becomes one enum containing ``null``.  Every answer is still checked by
    ``adapters._extract_responses``.
    """

    def narrow(value: Any) -> Any:
        if isinstance(value, list):
            return [narrow(item) for item in value]
        if not isinstance(value, dict):
            return value
        branches = value.get("anyOf")
        if set(value) == {"anyOf"} and isinstance(branches, list) and len(branches) == 2 and {"type": "null"} in branches:
            other = narrow(next(branch for branch in branches if branch != {"type": "null"}))
            if other.get("type") == "string" and "pattern" in other:
                return other
            if enum_nulls and set(other) == {"type", "enum"} and other["type"] == "string":
                return {"enum": [*other["enum"], None]}
        return {key: narrow(item) for key, item in value.items() if key != "minLength"}

    return narrow(copy.deepcopy(adapters.response_schema(packet)))


def union_parameters(schema: Any) -> int:
    """Count ``anyOf`` and type-array parameters, as Anthropic's limit does."""
    if isinstance(schema, list):
        return sum(union_parameters(item) for item in schema)
    if not isinstance(schema, dict):
        return 0
    own = int("anyOf" in schema or isinstance(schema.get("type"), list))
    return own + sum(union_parameters(item) for item in schema.values())


def response_name(packet: Mapping[str, Any]) -> str:
    return "ua_gec_responses" if packet.get("schema") == gec.GEC_PACKET_SCHEMA else "zno_nmt_responses"


def estimate_input_tokens(params: Mapping[str, Any]) -> int:
    """Conservative input estimate: one token per two UTF-8 bytes of the request.

    Real tokenizers give at least one Cyrillic letter (two bytes) per token, so
    this over-counts Ukrainian text and JSON.  It covers the prompt and the
    schema, which structured outputs also bill as input.
    """
    return math.ceil(len(adapters.canonical(params).encode("utf-8")) / 2) + 64


def request_upper_bound_usd(config: Mapping[str, Any], params: Mapping[str, Any], *, batch: bool) -> Decimal:
    """Estimated input plus the full output allowance (which includes thinking)."""
    price_in, price_out = prices(config)
    cost = (estimate_input_tokens(params) * price_in + config["max_output_tokens"] * price_out) / _MILLION
    return cost * BATCH_PRICE_FACTOR if batch else cost


def usage_cost(config: Mapping[str, Any], usage: Mapping[str, Any], *, batch: bool) -> Decimal:
    """List-price estimate of one billed response.

    Cache writes (unused here) are charged at twice the input price and cache
    reads at the full input price, so the figure does not under-count.
    """
    price_in, price_out = prices(config)
    tokens_in = usage["input_tokens"] + usage.get("cache_read_input_tokens", 0)
    tokens_in += 2 * usage.get("cache_creation_input_tokens", 0)
    cost = (tokens_in * price_in + usage["output_tokens"] * price_out) / _MILLION
    return cost * BATCH_PRICE_FACTOR if batch else cost


def as_dict(value: Any) -> dict[str, Any]:
    """Convert an SDK model (or a plain test mapping) to JSON data."""
    if isinstance(value, Mapping):
        return json.loads(adapters.canonical(value))
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    raise AdapterError("API response object is invalid")


def sdk_reason(exc: BaseException) -> str:
    """Normalise SDK and transport failures without their message text."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return f"http_status_{status}"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "connection" in name:
        return "network_error"
    return adapters.normalized_reason(exc)


_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})")


def _model_matches(requested: str, effective: Any) -> bool:
    """Accept the requested id or its dated snapshot (-YYYY-MM-DD or -YYYYMMDD), never another model."""
    if not isinstance(effective, str):
        return False
    return effective == requested or (
        effective.startswith(requested) and _SNAPSHOT_SUFFIX.fullmatch(effective[len(requested):]) is not None
    )


def check_model(config: Mapping[str, Any], effective: Any) -> str:
    if not _model_matches(config["model"], effective):
        raise AdapterError("API model drift")
    return effective


BatchOutcome = tuple[dict[str, Any] | None, str | None]


def attribute_batch(entries: list[tuple[Any, BatchOutcome, Any]]) -> tuple[dict[str, BatchOutcome], list[Any]]:
    """Map batch entries to custom ids without letting one bad entry discard the rest.

    An entry without a usable id cannot be attributed and is returned for the
    evidence record.  An id seen more than once is ambiguous: it fails, and all
    its entries are kept as evidence.  Missing ids are failed by the caller.
    """
    by_id: dict[str, list[tuple[BatchOutcome, Any]]] = {}
    unattributed: list[Any] = []
    for custom_id, outcome, raw in entries:
        if isinstance(custom_id, str) and custom_id:
            by_id.setdefault(custom_id, []).append((outcome, raw))
        else:
            unattributed.append(raw)
    results: dict[str, BatchOutcome] = {}
    for custom_id, found in by_id.items():
        if len(found) == 1:
            results[custom_id] = found[0][0]
        else:
            results[custom_id] = (None, "batch_duplicate_result")
            unattributed.extend(raw for _outcome, raw in found)
    return results, unattributed


# Malformed untrusted data can raise any of these: invalid or duplicate JSON,
# an integer beyond the digit limit (ValueError), deep nesting (RecursionError),
# or an unexpected type or shape.
MALFORMED = (TypeError, KeyError, AttributeError, ValueError, RecursionError)


def parse(reader: Callable[..., Any], *args: Any) -> Any:
    """Run a provider reader on untrusted response data; any malformation is an AdapterError."""
    try:
        return reader(*args)
    except AdapterError:
        raise
    except MALFORMED as exc:
        raise AdapterError("API response output is invalid") from exc


def batch_line(line: str) -> dict[str, Any] | None:
    """Decode one untrusted batch result line; None when it is not one JSON object."""
    try:
        value = adapters._strict_json_loads(line)
    except MALFORMED:
        return None
    return value if isinstance(value, dict) else None


def settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """The fixed per-call settings recorded with every result."""
    return {
        "model": config["model"],
        "effort": config["effort"],
        "temperature": config["temperature"],
        "max_output_tokens": config["max_output_tokens"],
        "timeout_seconds": config["timeout_seconds"],
    }


def implementation_sha256(provider: ModuleType) -> str:
    """Bind this module and the provider adapter source, like other adapters' hashes."""
    hasher = hashlib.sha256()
    for path in (Path(__file__), Path(provider.__file__)):
        hasher.update(hashlib.sha256(path.read_bytes()).digest())
    return hasher.hexdigest()


def _provider_module(adapter: str) -> ModuleType:
    if adapter == "anthropic-api":
        from . import api_anthropic

        return api_anthropic
    from . import api_openai

    return api_openai


def run_api_trial(
    packet: Mapping[str, Any], config: Mapping[str, Any], condition: str, *, prompt: str,
    evidence: Callable[[str, Any], None] | None = None,
) -> dict[str, Any]:
    """Runner-path trial: one synchronous request under ``max_usd_per_trial``."""
    checked = validate_api_config(config)
    if condition != "closed-book":
        raise AdapterError("direct API capability is closed-book only")
    provider = _provider_module(checked["adapter"])
    params = provider.build_request(packet, checked, prompt)
    bound = request_upper_bound_usd(checked, params, batch=False)
    if checked.get("max_usd_per_trial") is None:
        raise AdapterError("paid API trial configuration requires max_usd_per_trial")
    if bound > _decimal(checked["max_usd_per_trial"], "max_usd_per_trial"):
        raise AdapterError("API request budget refused: estimate exceeds max_usd_per_trial")
    if evidence is not None:
        evidence("api_request", {"params": params, "estimated_usd_upper_bound": str(bound)})
    started = time.monotonic()
    client = provider.make_client()
    try:
        raw = provider.call(client, params, checked["timeout_seconds"])
    except Exception as exc:  # SDK errors are normalised; their text may hold request detail
        raise AdapterError("API response " + sdk_reason(exc)) from exc
    if evidence is not None:
        evidence("api_response", raw)
    usage = parse(provider.read_usage, raw)
    metrics = _metrics(usage, usage_cost(checked, usage, batch=False), time.monotonic() - started)
    try:
        answer = parse(provider.read_answers, raw, packet, checked)
    except AdapterError as exc:
        # The response was billed: keep its usage, cost and settings in a failed trial.
        returned = raw.get("model")
        return {
            "status": "failed",
            "failure_reason": adapters.normalized_reason(exc),
            "responses": {str(item["id"]): None for item in packet["items"]},
            "identity": _identity(checked, effective_model=returned if isinstance(returned, str) else "unknown",
                                  session_id=raw.get("id") if isinstance(raw.get("id"), str) else None, mode="sync"),
            "metrics": metrics,
        }
    return {
        "responses": answer["responses"],
        "identity": _identity(checked, effective_model=answer["model"], session_id=answer["id"], mode="sync"),
        "metrics": metrics,
    }


def _identity(config: Mapping[str, Any], *, effective_model: str, session_id: str | None, mode: str) -> dict[str, Any]:
    spec = _PROVIDERS[config["adapter"]]
    return {
        "adapter": config["adapter"],
        "harness": spec["batch"] if mode == "batch" else spec["harness"],
        "model": config["model"],
        "provider": config["provider"] or config["adapter"],
        "session_id": session_id,
        "requested_model": config["model"],
        "effective_model": effective_model,
        "requested_effort": config["effort"],
        "effective_effort": "unknown",
        "temperature": config["temperature"],
        "max_output_tokens_configured": config["max_output_tokens"],
        "cli_version": None,
        "tool_schema_sha256": digest([]),
        "mcp_server_identity_sha256": None,
        "corpus_id_sha256": digest(config["corpus_id"]) if config["corpus_id"] is not None else None,
        "mode": mode,
    }


def _metrics(usage: Mapping[str, Any] | None, cost: Decimal | None, elapsed: float | None) -> dict[str, Any]:
    usage = usage or {}
    return {
        "elapsed_seconds": elapsed,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost_usd": float(cost) if cost is not None else None,
        "tool_calls": 0,
    }


# ---------------------------------------------------------------- suite command


def _load_packet(path: Path) -> dict[str, Any]:
    packet = read_json(path)
    if isinstance(packet, dict) and packet.get("schema") == gec.GEC_PACKET_SCHEMA:
        gec.validate_gec_packet(packet)
    else:
        validate_packet(packet)
    return packet


def _units(packet: dict[str, Any], plan_path: Path | None) -> tuple[list[dict[str, Any]], str | None]:
    """Request units: the whole packet, or each segment of a frozen plan."""
    ids = [str(item["id"]) for item in packet["items"]]
    if plan_path is None:
        return [{"segment_id": "all", "item_ids": ids, "local_ids": ids, "packet": packet}], None
    from .segmentation import _validate_plan_without_packet, derive_segment_packet

    plan = _validate_plan_without_packet(read_json(plan_path))
    if plan["source_packet_sha256"] != packet["packet_sha256"]:
        raise ExamError("segment plan is bound to a different packet")
    units = []
    covered: list[str] = []
    for segment in plan["segments"]:
        unit_packet = derive_segment_packet(packet, list(segment["item_ids"]))
        if unit_packet["packet_sha256"] != segment["packet_sha256"]:
            raise ExamError("segment packet hash mismatch")
        local_ids = [str(item["id"]) for item in unit_packet["items"]]
        units.append({"segment_id": segment["segment_id"], "item_ids": list(segment["item_ids"]),
                      "local_ids": local_ids, "packet": unit_packet})
        covered.extend(segment["item_ids"])
    if covered != ids:
        raise ExamError("segment plan does not cover the packet exactly")
    return units, plan["segment_plan_sha256"]


def _requests(provider: ModuleType, config: dict[str, Any], units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requests = []
    for repeat in range(1, config["repeats"] + 1):
        for index, unit in enumerate(units):
            prompt = adapters.build_prompt(unit["packet"], "closed-book", max_tool_calls=config["max_tool_calls"])
            params = provider.build_request(unit["packet"], config, prompt)
            requests.append({"custom_id": f"r{repeat:03d}-{unit['segment_id']}", "repeat": repeat,
                             "unit": index, "params": params})
    return requests


def _estimate(config: dict[str, Any], requests: list[dict[str, Any]], *, batch: bool) -> dict[str, Any]:
    price_in, price_out = prices(config)
    input_tokens = sum(estimate_input_tokens(request["params"]) for request in requests)
    total = sum((request_upper_bound_usd(config, request["params"], batch=batch) for request in requests), Decimal(0))
    factor = BATCH_PRICE_FACTOR if batch else Decimal(1)
    return {
        "requests": len(requests),
        "estimated_input_tokens": input_tokens,
        "max_output_tokens_per_request": config["max_output_tokens"],
        "estimated_input_usd": float(input_tokens * price_in * factor / _MILLION),
        "estimated_usd_upper_bound": float(total),
        "usd_per_mtok": {"input": str(price_in * factor), "output": str(price_out * factor)},
        "price_source": "configuration" if "usd_per_mtok" in config else f"list price checked {PRICES_CHECKED}",
    }


def _private_dir(path: Path) -> None:
    if path.exists():
        raise ExamError("output directory must not exist; every suite run is new")
    path.mkdir(mode=0o700, parents=True)


def _row(request: dict[str, Any], config: dict[str, Any], mode: str, raw: Any, failure: str | None,
         elapsed: float | None, provider: ModuleType, unit_packet: Mapping[str, Any]) -> dict[str, Any]:
    """One result row: settings, usage and cost are recorded even when answers fail."""
    usage = cost = None
    responses = None
    effective = "unknown"
    session = None
    if raw is not None:
        try:
            usage = parse(provider.read_usage, raw)
            cost = usage_cost(config, usage, batch=mode == "batch")
        except AdapterError as exc:
            failure = failure or adapters.normalized_reason(exc)
        try:
            answer = parse(provider.read_answers, raw, unit_packet, config)
            responses, effective, session = answer["responses"], answer["model"], answer["id"]
        except AdapterError as exc:
            failure = failure or adapters.normalized_reason(exc)
    if failure is not None:
        responses = None
    return {
        "schema": ROW_SCHEMA,
        "custom_id": request["custom_id"],
        "repeat": request["repeat"],
        "status": "failed" if failure is not None else "ok",
        "failure_reason": failure,
        "responses": responses,
        "settings": settings(config),
        "identity": _identity(config, effective_model=effective, session_id=session, mode=mode),
        "metrics": _metrics(usage, cost, elapsed),
    }


def _run_object(packet: dict[str, Any], config: dict[str, Any], repeat: int, rows: list[dict[str, Any]],
                units: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    """Reassemble one repeat into the existing run schema for offline scoring."""
    from .runner import _comparison, _run_schema

    responses: dict[str, Any] = {}
    for unit, row in zip(units, rows, strict=True):
        for local_id, source_id in zip(unit["local_ids"], unit["item_ids"], strict=True):
            responses[source_id] = row["responses"][local_id] if row["status"] == "ok" else None
    failures = [row["failure_reason"] for row in rows if row["status"] != "ok"]
    effective = sorted({row["identity"]["effective_model"] for row in rows if row["status"] == "ok"})

    def total(field: str) -> Any:
        values = [row["metrics"][field] for row in rows]
        return None if any(value is None for value in values) else sum(values)

    # Every ok row already passed the model check; dated snapshots may still differ.
    identity = _identity(config, effective_model=effective[0] if len(effective) == 1 else "unknown",
                         session_id=None, mode=mode)
    identity["effective_models"] = effective
    identity["session_ids"] = [row["identity"]["session_id"] for row in rows]
    identity["request_count"] = len(rows)
    run = {
        "schema": _run_schema(packet),
        "packet_sha256": packet["packet_sha256"],
        "condition": "closed-book",
        "status": "failed" if failures else "ok",
        "repeat": repeat,
        "responses": responses,
        "identity": identity,
        "comparison": _comparison(packet, config),
        "metrics": {field: total(field) for field in
                    ("elapsed_seconds", "input_tokens", "output_tokens", "total_tokens", "cost_usd", "tool_calls")},
    }
    if failures:
        run["failure_reason"] = failures[0]
    return run


def _attempt_metadata(packet: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    return {"denominator": len(packet["items"]), "packet_sha256": packet["packet_sha256"],
            "config_sha256": digest(config), "condition": "closed-book",
            "route_sha256": digest({"adapter": config["adapter"], "model": config["model"]})}


def _attempt_id(repeat: int) -> str:
    return f"repeat-{repeat:03d}"


def _record_response(store: EvidenceStore, request: dict[str, Any], raw: Any, failure: str | None) -> None:
    store.append_event(_attempt_id(request["repeat"]), "api_response",
                       {"custom_id": request["custom_id"], "raw": raw, "transport_failure": failure})


def _finish(out: Path, suite: dict[str, Any], provider: ModuleType,
            results: dict[str, tuple[Any, str | None, float | None]]) -> dict[str, Any]:
    """Write rows, finalize one evidence attempt and one run file per repeat."""
    packet, config, units, mode = suite["packet"], suite["config"], suite["units"], suite["mode"]
    store = EvidenceStore(out / "evidence")
    by_repeat: dict[int, list[dict[str, Any]]] = {}
    for request in suite["requests"]:
        raw, failure, elapsed = results.get(request["custom_id"], (None, "batch_missing_result", None))
        row = _row(request, config, mode, raw, failure, elapsed, provider, units[request["unit"]]["packet"])
        if mode == "batch":  # synchronous responses were recorded as each arrived
            _record_response(store, request, raw, failure)
        write_private_json(out / "rows" / f"{request['custom_id']}.json", row)
        by_repeat.setdefault(request["repeat"], []).append(row)
    summary = {"status": "ok", "requests": len(suite["requests"]), "ok": 0, "failed": 0, "cost_usd": 0.0, "runs": []}
    for repeat, rows in sorted(by_repeat.items()):
        run = _run_object(packet, config, repeat, rows, units, mode)
        store.finalize(_attempt_id(repeat), run, status="completed" if run["status"] == "ok" else "failed")
        write_private_json(out / "runs" / f"{_attempt_id(repeat)}.json", run)
        summary["runs"].append({"repeat": repeat, "status": run["status"], "attempt_id": _attempt_id(repeat)})
        for row in rows:
            summary["ok" if row["status"] == "ok" else "failed"] += 1
            summary["cost_usd"] += row["metrics"]["cost_usd"] or 0.0
    if summary["failed"]:
        summary["status"] = "failed"
    return summary


def _start(args: argparse.Namespace, provider: ModuleType) -> int:
    config = validate_api_config(read_json(args.config))
    if config["adapter"] != provider.ADAPTER:
        raise ExamError(f"configuration adapter must be {provider.ADAPTER}")
    packet = _load_packet(args.questions)
    units, plan_sha256 = _units(packet, args.segment_plan)
    requests = _requests(provider, config, units)
    mode = "batch" if args.batch else "sync"
    estimate = _estimate(config, requests, batch=args.batch)
    report = {"adapter": config["adapter"], "model": config["model"], "mode": mode,
              "settings": settings(config), **estimate}
    if args.dry_run:
        print(json.dumps({"status": "dry_run", **report}, ensure_ascii=False))
        return 0
    if args.max_usd is None:
        raise ExamError("--max-usd is required for a paid run")
    max_usd = _decimal(args.max_usd, "--max-usd")
    per_trial = config.get("max_usd_per_trial")
    over_trial = per_trial is not None and any(
        request_upper_bound_usd(config, request["params"], batch=args.batch) > _decimal(per_trial, "max_usd_per_trial")
        for request in requests)
    bound = sum((request_upper_bound_usd(config, request["params"], batch=args.batch) for request in requests),
                Decimal(0))
    if bound > max_usd or over_trial:
        print(json.dumps({"status": "refused", "reason": "estimated_cost_exceeds_max_usd",
                          "max_usd": str(max_usd), **report}, ensure_ascii=False))
        return 2
    client = provider.make_client()
    _private_dir(args.out_dir)
    suite = {"schema": SUITE_SCHEMA, "mode": mode, "config": config, "config_sha256": digest(config),
             "packet": packet, "segment_plan_sha256": plan_sha256, "units": units, "requests": requests,
             "max_usd": str(max_usd), "estimate": estimate,
             "implementation_sha256": implementation_sha256(provider)}
    write_private_json(args.out_dir / "suite.json", suite)
    store = EvidenceStore(args.out_dir / "evidence")
    for repeat in range(1, config["repeats"] + 1):
        attempt = store.start(_attempt_metadata(packet, config), attempt_id=_attempt_id(repeat))
        for request in requests:
            if request["repeat"] == repeat:
                attempt.append("api_request", {"custom_id": request["custom_id"], "params": request["params"]})
    if args.batch:
        batch_id = provider.submit_batch(client, [(request["custom_id"], request["params"]) for request in requests])
        write_private_json(args.out_dir / "batch.json", {"batch_id": batch_id, "submitted_at": time.time()})
        print(json.dumps({"status": "submitted", "batch_id": batch_id, **report}, ensure_ascii=False))
        return 0
    results: dict[str, tuple[Any, str | None, float | None]] = {}
    for request in requests:
        started = time.monotonic()
        try:
            raw, failure = provider.call(client, request["params"], config["timeout_seconds"]), None
        except Exception as exc:  # noqa: BLE001 - one fresh request each; failure kept as a row, never retried
            raw, failure = None, sdk_reason(exc)
        results[request["custom_id"]] = (raw, failure, time.monotonic() - started)
        # Persist every billed response at once, so an interrupted run keeps it.
        _record_response(store, request, raw, failure)
    summary = _finish(args.out_dir, suite, provider, results)
    print(json.dumps({**summary, "estimated_usd_upper_bound": estimate["estimated_usd_upper_bound"]}))
    return 0 if summary["status"] == "ok" else 2


def _collect(args: argparse.Namespace, provider: ModuleType) -> int:
    suite = read_json(args.out_dir / "suite.json")
    if suite.get("schema") != SUITE_SCHEMA or suite.get("mode") != "batch":
        raise ExamError("output directory does not hold a submitted batch suite")
    if suite["config"]["adapter"] != provider.ADAPTER:
        raise ExamError(f"suite adapter must be {provider.ADAPTER}")
    if (args.out_dir / "runs").exists():
        raise ExamError("batch results were already collected")
    batch_id = read_json(args.out_dir / "batch.json")["batch_id"]
    client = provider.make_client()
    deadline = time.monotonic() + args.wait_seconds
    while not provider.batch_ended(client, batch_id):
        if time.monotonic() >= deadline:
            print(json.dumps({"status": "in_progress", "batch_id": batch_id}))
            return 3
        time.sleep(min(60.0, max(1.0, deadline - time.monotonic())))
    outcomes, unattributed = provider.batch_results(client, batch_id)
    expected = {request["custom_id"] for request in suite["requests"]}
    unattributed += [{"custom_id": custom_id, "outcome": list(outcome)}
                     for custom_id, outcome in outcomes.items() if custom_id not in expected]
    if unattributed:  # kept as evidence; the affected requests fail as missing or duplicate
        write_private_json(args.out_dir / "batch-unattributed.json", unattributed)
    results = {custom_id: (raw, failure, None) for custom_id, (raw, failure) in outcomes.items()}
    summary = _finish(args.out_dir, suite, provider, results)
    print(json.dumps({**summary, "batch_id": batch_id, "unattributed_entries": len(unattributed)}))
    return 0 if summary["status"] == "ok" else 2


def parser(provider: ModuleType) -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        prog=f"python -m ukrainian_llm_eval.{Path(provider.__file__).stem}",
        description=f"Run a question packet through the {provider.ADAPTER} direct API (closed-book). "
        "Writes rows, runs and evidence for offline scoring; never accepts a grading key.",
        epilog="Keys come only from the SDK's standard environment variable. Exit 0: success; "
        "2: refused, invalid input or failed request; 3: batch still in progress.",
    )
    commands = cli.add_subparsers(dest="command", required=True)
    suite = commands.add_parser("suite", help="Estimate, then run synchronously or submit a batch")
    suite.add_argument("--config", type=Path, required=True)
    suite.add_argument("--questions", type=Path, required=True)
    suite.add_argument("--segment-plan", type=Path, help="Frozen segment plan: one request per segment")
    suite.add_argument("--out-dir", type=Path, help="New private output directory (required unless --dry-run)")
    suite.add_argument("--max-usd", help="Refuse before any request if the estimate exceeds this (required)")
    suite.add_argument("--batch", action="store_true", help="Submit one provider batch at the batch price")
    suite.add_argument("--dry-run", action="store_true", help="Print request count and estimate; no key or call")
    collect = commands.add_parser("collect", help="Collect a submitted batch into rows, runs and evidence")
    collect.add_argument("--out-dir", type=Path, required=True)
    collect.add_argument("--wait-seconds", type=int, default=0, help="Poll until the batch ends or this elapses")
    return cli


def main(provider: ModuleType, argv: list[str] | None = None) -> int:
    args = parser(provider).parse_args(argv)
    try:
        if args.command == "suite":
            if not args.dry_run and args.out_dir is None:
                raise ExamError("--out-dir is required for a paid run")
            return _start(args, provider)
        return _collect(args, provider)
    except (ExamError, ValueError, OSError) as exc:
        # Only this package's own messages are printed; SDK error text never is.
        report = {"status": "failed", "error_class": type(exc).__name__}
        if type(exc) in {ExamError, AdapterError}:
            report["error"] = str(exc)
        print(json.dumps(report))
        return 2


__all__ = ["LIST_PRICES_USD_PER_MTOK", "main", "preflight", "run_api_trial", "validate_api_config"]
