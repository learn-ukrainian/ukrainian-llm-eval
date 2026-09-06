"""One-trial execution contract for the ZNO/NMT evaluator.

Pairing and grading live above this module.  This module deliberately performs
one fresh session only: it never retries, repairs answers, selects a best run,
or accepts a grading key.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import adapters, gec
from .candidate_outcome import is_candidate_response_failure
from .core import ExamError, digest, validate_packet
from .request_budget import RequestBudgetError

GEC_RUN_SCHEMA = "ua-gec.run.v1"
GEC_COMPARISON_SCHEMA = "ua-gec.comparison.v1"
MCQ_RUN_SCHEMA = "zno-nmt.run.v1"


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a serialisable adapter configuration without reading secrets."""
    try:
        return adapters.validate_config(config)
    except adapters.AdapterError as exc:
        raise ExamError(str(exc)) from exc


def preflight(config: Mapping[str, Any], condition: str, sources_url: str | None = None) -> dict[str, Any]:
    """Fail before a provider run when route or tool controls are unproven."""
    try:
        return adapters.preflight(config, condition, sources_url)
    except adapters.AdapterError as exc:
        raise ExamError(str(exc)) from exc


def _validated_packet(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(packet, Mapping) and packet.get("schema") == gec.GEC_PACKET_SCHEMA:
        gec.validate_gec_packet(packet)
        return packet
    try:
        candidate = validate_packet(packet)
    except ExamError:
        raise
    except ValueError as exc:
        raise ExamError(str(exc)) from exc
    return candidate if isinstance(candidate, Mapping) else packet


def _run_schema(packet: Mapping[str, Any]) -> str:
    return GEC_RUN_SCHEMA if packet.get("schema") == gec.GEC_PACKET_SCHEMA else MCQ_RUN_SCHEMA


def _comparison(packet: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Stable pair constants; session identity and condition are excluded."""
    adapter_source = Path(adapters.__file__).read_bytes()
    runner_source = Path(__file__).read_bytes()
    proxy_source = Path(adapters.__file__).with_name("mcp_proxy.py").read_bytes()
    route: dict[str, Any] = {"provider": config.get("provider"), "adapter": config["adapter"]}
    if config["adapter"] in {"chat-http", "responses-http", "opencode"}:
        endpoint_env = config["endpoint_env"]
        route["endpoint_env_sha256"] = digest(endpoint_env)
        endpoint = os.environ.get(endpoint_env)
        route["endpoint_sha256"] = hashlib.sha256(endpoint.encode("utf-8")).hexdigest() if endpoint else None
    constants = {
        "adapter": config["adapter"],
        "model": config["model"],
        "effort": config["effort"],
        "timeout_seconds": config["timeout_seconds"],
        "max_output_tokens": config["max_output_tokens"],
        "max_tool_calls": config["max_tool_calls"],
        "repeats": config["repeats"],
        # Bind the actual prompt implementation and response-schema generator,
        # while deliberately excluding the only paired variable: tool policy.
        "prompt_implementation_sha256": hashlib.sha256(adapter_source).hexdigest(),
        "runner_implementation_sha256": hashlib.sha256(runner_source).hexdigest(),
        "mcp_proxy_implementation_sha256": hashlib.sha256(proxy_source).hexdigest(),
        "response_schema_sha256": digest(adapters.response_schema(packet)),
        "packet_sha256": packet["packet_sha256"],
        "packet_schema": packet["schema"],
        "configured_tools_sha256": digest(config["tools"]),
        "corpus_id_sha256": digest(config["corpus_id"]) if config["corpus_id"] is not None else None,
        "route": route,
    }
    if config["adapter"] == "opencode":
        constants["openrouter"] = config["openrouter"]
        for filename in ("native_opencode.py", "opencode_gateway.py"):
            constants[filename + "_sha256"] = hashlib.sha256(
                Path(adapters.__file__).with_name(filename).read_bytes()
            ).hexdigest()
    elif config["adapter"] in {"kimi", "codex"}:
        constants["native_adapter_implementation_sha256"] = hashlib.sha256(
            Path(adapters.__file__).with_name(f"native_{config['adapter']}.py").read_bytes()
        ).hexdigest()
    elif config["adapter"] == "responses-http":
        constants["http_adapter_implementation_sha256"] = hashlib.sha256(
            Path(adapters.__file__).with_name("responses_http.py").read_bytes()
        ).hexdigest()
    if packet.get("schema") == gec.GEC_PACKET_SCHEMA:
        gec_source = Path(gec.__file__).read_bytes()
        adapter_hash = hashlib.sha256(adapter_source).hexdigest()
        constants.update(
            {
                "response_implementation_sha256": adapter_hash,
                "gec_validator_implementation_sha256": hashlib.sha256(gec_source).hexdigest(),
            }
        )
        comparison_schema = GEC_COMPARISON_SCHEMA
    else:
        comparison_schema = "zno-nmt.comparison.v1"
    return {"schema": comparison_schema, "constants_sha256": digest(constants)}


def _empty_metrics() -> dict[str, Any]:
    return {
        "elapsed_seconds": None,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "tool_calls": None,
    }


def _failure(packet: Mapping[str, Any], config: Mapping[str, Any], condition: str, exc: BaseException) -> dict[str, Any]:
    return {
        "schema": _run_schema(packet),
        "packet_sha256": packet["packet_sha256"],
        "condition": condition,
        "status": "failed",
        "responses": {str(item["id"]): None for item in packet["items"]},
        "identity": {
            "adapter": config["adapter"],
            "harness": config["adapter"],
            "model": config["model"],
            "provider": config.get("provider"),
            "requested_model": config["model"],
            "effective_model": "unknown",
            "requested_effort": config["effort"],
            "effective_effort": "unknown",
            "session_id": None,
        },
        "comparison": _comparison(packet, config),
        "metrics": _empty_metrics(),
        "failure_reason": adapters.normalized_reason(exc),
    }


def run_exam(
    packet: Mapping[str, Any],
    config: Mapping[str, Any],
    condition: str,
    *,
    sources_url: str | None = None,
    evidence: Callable[[str, Any], None] | None = None,
    request_budget: Any = None,
) -> dict[str, Any]:
    """Run exactly one fresh exam session under the requested condition.

    A malformed packet/configuration remains a caller error.  Once those are
    valid, any preflight or provider failure becomes an immutable failed trial
    with only a normalized reason, never provider stdout/stderr or a secret.
    """
    checked_packet = _validated_packet(packet)
    checked_config = validate_config(config)
    if condition not in {"closed-book", "sources"}:
        raise ExamError("condition must be closed-book or sources")
    if evidence is not None:
        evidence("trial_input", {"packet": checked_packet, "config": checked_config, "condition": condition})
    try:
        capability = preflight(checked_config, condition, sources_url)
        if evidence is not None:
            evidence("preflight", capability)
        prompt = adapters.build_prompt(
            checked_packet, condition, max_tool_calls=checked_config["max_tool_calls"]
        )
        if evidence is not None:
            evidence("prompt", {"text": prompt, "response_schema": adapters.response_schema(checked_packet)})
        evidence_options = {"evidence": evidence} if evidence is not None else {}
        if checked_config["adapter"] == "claude":
            if request_budget is not None:
                raise ExamError("request-level budget is unavailable for the native CLI adapter")
            trial = adapters.run_claude(
                checked_packet,
                checked_config,
                condition,
                sources_url=sources_url,
                prompt=prompt,
                **evidence_options,
            )
        elif checked_config["adapter"] == "kimi":
            from .native_kimi import run_kimi

            if request_budget is not None:
                raise ExamError("request-level budget is unavailable for the native CLI adapter")
            trial = run_kimi(
                checked_packet, checked_config, condition, sources_url=sources_url, prompt=prompt,
                private_env_path=os.environ.get("UKRAINIAN_LLM_EVAL_KIMI_PROVISIONING_DIR"),
                **evidence_options,
            )
            for field in ("binary_sha256", "native_config_sha256", "catalog_provider_sha256", "catalog_model_sha256"):
                if trial["identity"].get(field) != capability.get(field):
                    raise ExamError("native Kimi configuration changed after preflight")
        elif checked_config["adapter"] == "opencode":
            from .native_opencode import run_opencode

            trial = run_opencode(
                checked_packet, checked_config, condition, sources_url=sources_url, prompt=prompt,
                request_budget=request_budget, **evidence_options,
            )
            for field in ("binary_sha256", "cli_version"):
                if trial["identity"].get(field) != capability.get(field):
                    raise ExamError("native OpenCode changed after preflight")
        elif checked_config["adapter"] == "codex":
            from .native_codex import run_codex

            if request_budget is not None:
                raise ExamError("request-level budget is unavailable for the native CLI adapter")
            trial = run_codex(
                checked_packet, checked_config, condition, sources_url=sources_url, prompt=prompt,
                private_env_path=os.environ.get("UKRAINIAN_LLM_EVAL_CODEX_PROVISIONING_DIR"),
                **evidence_options,
            )
            for field in (
                "entrypoint_sha256", "native_runtime_sha256", "control_receipt_sha256",
                "settings_sha256", "request_shape_sha256", "cli_version",
            ):
                if trial["identity"].get(field) != capability.get(field):
                    raise ExamError("native Codex configuration changed after preflight")
        else:
            budget_options = {"request_budget": request_budget} if request_budget is not None else {}
            if checked_config["adapter"] == "responses-http":
                from .responses_http import run_responses_http

                run_http = run_responses_http
            else:
                run_http = adapters.run_chat_http
            trial = run_http(
                checked_packet,
                checked_config,
                condition,
                sources_url=sources_url,
                prompt=prompt,
                **budget_options,
                **evidence_options,
            )
        if trial.get("status") == "failed":
            expected_ids = [str(item["id"]) for item in checked_packet["items"]]
            if not is_candidate_response_failure(trial, expected_response_ids=expected_ids):
                raise ExamError("candidate response failure evidence is invalid")
            identity = dict(trial["identity"])
            if request_budget is not None:
                budget_receipt = request_budget.finalize("failed")
                identity["request_budget_receipt_sha256"] = digest(budget_receipt)
            identity["preflight_tool_schema_sha256"] = capability["tool_schema_sha256"]
            identity["preflight_mcp_server_identity_sha256"] = capability["mcp_server_identity_sha256"]
            failure = {
                "schema": _run_schema(checked_packet),
                "packet_sha256": checked_packet["packet_sha256"],
                "condition": condition,
                "status": "failed",
                "responses": dict(trial["responses"]),
                "identity": identity,
                "comparison": _comparison(checked_packet, checked_config),
                "metrics": dict(trial["metrics"]),
                "failure_reason": trial["failure_reason"],
            }
            if evidence is not None:
                evidence("trial_failure", failure)
            return failure
    except (adapters.AdapterError, ExamError, RequestBudgetError, OSError, TimeoutError) as exc:
        budget_receipt = None
        if request_budget is not None:
            budget_receipt = request_budget.finalize("failed")
        failure = _failure(checked_packet, checked_config, condition, exc)
        if budget_receipt is not None:
            failure["identity"]["request_budget_receipt_sha256"] = digest(budget_receipt)
        if evidence is not None:
            evidence("trial_failure", failure)
        return failure
    identity = dict(trial["identity"])
    if request_budget is not None:
        budget_receipt = request_budget.finalize("completed")
        identity["request_budget_receipt_sha256"] = digest(budget_receipt)
    identity["preflight_tool_schema_sha256"] = capability["tool_schema_sha256"]
    identity["preflight_mcp_server_identity_sha256"] = capability["mcp_server_identity_sha256"]
    return {
        "schema": _run_schema(checked_packet),
        "packet_sha256": checked_packet["packet_sha256"],
        "condition": condition,
        "status": "ok",
        "responses": trial["responses"],
        "identity": identity,
        "comparison": _comparison(checked_packet, checked_config),
        "metrics": dict(trial["metrics"]),
    }
