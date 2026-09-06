"""Resume a frozen paired schedule without retrying or dropping started attempts."""

from __future__ import annotations

import contextlib
import hashlib
import os
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from .candidate_outcome import is_candidate_response_failure
from .core import ExamError, digest, read_json, write_private_json
from .evidence import EvidenceStore
from .execution import execute_attempt, route_fingerprint
from .runner import _failure, preflight


def research_implementation_sha256():
    """Bind the actual controller/prompt/tool implementation, not a label."""
    names = (
        "adapters.py", "runner.py", "mcp_proxy.py", "execution.py", "scheduling.py", "segmentation.py",
        "admission.py", "admission_command.py", "request_budget.py", "spending_ledger.py",
        "benchmark_manifest.py", "core.py",
        "evidence.py", "gec.py",
        "native_kimi.py", "native_codex.py", "responses_http.py", "candidate_outcome.py",
    )
    return digest({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names})


def _research_config(base, suite, repeats):
    from .runner import validate_config

    return validate_config({**base, **suite["limits"], "repeats": repeats})


def _immutable_json(path, payload):
    if path.exists():
        if read_json(path) != payload:
            raise ExamError("existing research artifact differs from preserved evidence")
    else:
        write_private_json(path, payload)


def _research_stop_reason(result, route, config):
    """An observed overrun/identity drift stops the entire experiment."""
    if result.get("failure_reason") == "request_budget_error":
        return "request_budget_overrun"
    if result.get("status") != "ok" and not is_candidate_response_failure(result):
        return None
    identity = result.get("identity", {})
    observed_model = identity.get("effective_model", identity.get("model"))
    if observed_model not in (None, "unknown", config["model"]):
        context_mapping = identity.get("model_context_mapping")
        # The native adapter emits this only after checking initial selector,
        # terminal canonicalModel, and the actual contextWindow together.
        if not (config.get("adapter") == "claude" and identity.get("adapter") == "claude"
                and isinstance(observed_model, str) and config["model"] == observed_model + "[1m]"
                and context_mapping == {config["model"]: observed_model}):
            return "model_identity_drift"
    observed_effort = identity.get("effective_effort")
    if observed_effort not in (None, "unknown", config["effort"]):
        return "effort_identity_drift"
    metrics = result.get("metrics", {})
    tools = metrics.get("tool_calls")
    if tools is not None and (type(tools) is not int or tools < 0 or tools > config["max_tool_calls"]):
        return "tool_budget_overrun"
    billing = route["billing"]
    for observed, limit in (("input_tokens", "max_total_input_tokens"),
                            ("output_tokens", "max_total_output_tokens")):
        value = metrics.get(observed)
        if value is not None and (type(value) is not int or value < 0 or value > billing[limit]):
            return "token_reservation_overrun"
    return None


def _observed_micro_usd(result, route):
    # Native subscription cost estimates are not incremental charges.
    if route["billing"]["kind"] != "metered":
        return None
    value = result.get("metrics", {}).get("cost_usd")
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ExamError("invalid observed cost")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ExamError("invalid observed cost")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


@contextlib.contextmanager
def _lock(root):
    try:
        import fcntl
    except ImportError as exc:
        raise ExamError("paired scheduling requires POSIX file locks") from exc
    fd = os.open(root / ".execution.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExamError("this schedule is already running") from exc
        yield
    finally:
        os.close(fd)


def _pair_metadata(packet, config, condition, sources_url):
    return {"denominator": len(packet["items"]), "packet_sha256": packet["packet_sha256"],
            "config_sha256": digest(config), "condition": condition,
            "route_sha256": route_fingerprint(config, sources_url)}


def _pair_session(result, expected_response_ids):
    """Return the candidate-failure classification and observed session id."""
    candidate_failed = is_candidate_response_failure(
        result, expected_response_ids=expected_response_ids
    )
    if result.get("status") != "ok" and not candidate_failed:
        return candidate_failed, None
    identity = result.get("identity", {})
    return candidate_failed, identity.get("session_id") if isinstance(identity, dict) else None


def _pair_stop(root, plan, slot, receipt):
    stop = {"plan_sha256": digest(plan), "reason": "missing_or_reused_session",
            "attempt_id": slot, "receipt_sha256": digest(receipt)}
    _immutable_json(root / "stop.json", stop)
    return {"status": "stopped", "reason": stop["reason"], "failed": True}


def run_pair(packet, config, root: Path, *, sources_url=None, resume=False):
    schedule = [
        {"repeat": repeat, "condition": condition}
        for repeat in range(1, config["repeats"] + 1)
        for condition in (("closed-book", "sources") if repeat % 2 else ("sources", "closed-book"))
    ]
    plan = {"schema": "zno-nmt.plan.v1", "packet_sha256": packet["packet_sha256"],
            "config": config, "config_sha256": digest(config), "schedule": schedule,
            "route_sha256": route_fingerprint(config, sources_url)}
    if root.is_symlink():
        raise ExamError("schedule directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=resume)
    with _lock(root):
        if resume:
            if read_json(root / "plan.json") != plan:
                raise ExamError("resume inputs differ from frozen schedule")
        else:
            write_private_json(root / "plan.json", plan)
        store = EvidenceStore(root / "evidence")
        existing = store.verify_all()
        slots = {f"r{trial['repeat']:03d}-{trial['condition']}" for trial in schedule}
        if set(existing) - slots:
            raise ExamError("evidence contains an attempt outside the frozen schedule")
        stop_path = root / "stop.json"
        if stop_path.exists():
            stop = read_json(stop_path)
            if not isinstance(stop, dict) or set(stop) != {"plan_sha256", "reason", "attempt_id", "receipt_sha256"}:
                raise ExamError("invalid paired stop record")
            if any(not isinstance(stop[field], str) for field in stop):
                raise ExamError("invalid paired stop record")
            if stop["reason"] != "missing_or_reused_session":
                raise ExamError("invalid paired stop reason")
            if stop["plan_sha256"] != digest(plan):
                raise ExamError("stop record does not match frozen schedule")
            if stop["attempt_id"] not in slots:
                raise ExamError("stop record references an attempt outside the frozen schedule")
            receipt = existing.get(stop["attempt_id"])
            if receipt is None or not receipt["complete"]:
                raise ExamError("stop record receipt is missing or incomplete")
            if digest(receipt) != stop["receipt_sha256"]:
                raise ExamError("stop record receipt binding mismatch")
            yield {"status": "stopped", "reason": stop["reason"], "failed": True}
            return
        expected_response_ids = [item["id"] for item in packet["items"]]
        seen_sessions = set()
        # Validate every preserved eligible result before preflight or any new
        # provider execution. Generic and interrupted failures deliberately do
        # not participate in the fresh-session policy.
        for trial in schedule:
            slot = f"r{trial['repeat']:03d}-{trial['condition']}"
            receipt = existing.get(slot)
            if receipt is None:
                continue
            expected = _pair_metadata(packet, config, trial["condition"], sources_url)
            if receipt["metadata"] != expected:
                raise ExamError("attempt does not match frozen schedule")
            if not receipt["complete"]:
                continue
            candidate_failed, session_id = _pair_session(receipt["result"], expected_response_ids)
            if receipt["result"]["status"] == "ok" or candidate_failed:
                if not isinstance(session_id, str) or not session_id or session_id in seen_sessions:
                    yield _pair_stop(root, plan, slot, receipt)
                    return
                seen_sessions.add(session_id)
        # Check both conditions before any new provider execution.
        if slots - set(existing):
            for condition in ("closed-book", "sources"):
                preflight(config, condition, sources_url)
        failed = False
        for trial in schedule:
            slot = f"r{trial['repeat']:03d}-{trial['condition']}"
            receipt = existing.get(slot)
            if receipt is not None:
                expected = _pair_metadata(packet, config, trial["condition"], sources_url)
                if receipt["metadata"] != expected:
                    raise ExamError("attempt does not match frozen schedule")
                if not receipt["complete"]:
                    # The schedule lock proves no cooperating executor still owns this slot.
                    # Preserve the original events and account for interruption as failure.
                    result = _failure(packet, config, trial["condition"], TimeoutError())
                    result["failure_reason"] = "interrupted"
                    receipt = store.finalize(slot, result, status="interrupted")
                result = receipt["result"]
            else:
                result, receipt = execute_attempt(packet, config, trial["condition"], root / "evidence",
                                                  sources_url=sources_url, attempt_id=slot)
            result = {**result, "repeat": trial["repeat"]}
            for path, payload in [
                (root / f"{trial['repeat']:03d}-{trial['condition']}.json", result),
                (root / f"{trial['repeat']:03d}-{trial['condition']}.evidence.json", receipt),
            ]:
                if path.exists():
                    if read_json(path) != payload:
                        raise ExamError("existing result differs from preserved evidence")
                else:
                    write_private_json(path, payload)
            candidate_failed, session_id = _pair_session(result, expected_response_ids)
            if slot not in existing and (result["status"] == "ok" or candidate_failed):
                if not isinstance(session_id, str) or not session_id or session_id in seen_sessions:
                    yield _pair_stop(root, plan, slot, receipt)
                    return
                seen_sessions.add(session_id)
            failed |= result["status"] != "ok"
            yield {**trial, "status": result["status"], "resumed": slot in existing, "failed": failed}
            if result["status"] != "ok" and slot not in existing and not candidate_failed:
                return


def run_research(packets, segment_plans, manifest, plan, configs, root: Path, *,
                 admission_probe, request_budget_controller=None, sources_urls=None, resume=False):
    """Run the complete frozen experiment, retaining every started reservation.

    ``admission_probe`` implements the trusted controller protocol, including
    whole-schedule preparation and fresh per-segment evidence. A manifest hash
    alone is not proof of current authorization or cost.
    The experiment-wide POSIX lock covers admission, allocation and execution.
    """
    from .adapters import build_prompt
    from .admission import admission_composite_sha256, build_admission_request, verify_admission_evidence
    from .benchmark_manifest import validate_execution_plan
    from .request_budget import request_budget_attempt_id, verify_request_budget_evidence
    from .runner import _comparison, _validated_packet
    from .segmentation import derive_segment_packet, reassemble_cell, validate_segment_plan
    from .spending_ledger import SpendingCapExceeded

    validate_execution_plan(manifest, plan)
    if not callable(admission_probe) or not callable(getattr(admission_probe, "prepare", None)):
        raise ExamError("research execution requires a live admission probe")
    if manifest["tool_policy_sha256"] != research_implementation_sha256():
        raise ExamError("research controller implementation drift")
    suites = {suite["suite_id"]: suite for suite in manifest["suites"]}
    routes = {route["route_id"]: route for route in manifest["routes"]}
    paid_routes = {route_id for route_id, route in routes.items()
                   if route["billing"]["kind"] in {"metered", "existing_credit"}}
    if paid_routes and request_budget_controller is None:
        raise ExamError("metered and existing-credit research requires request-level budgeting")
    budgeted_routes = paid_routes | {route_id for route_id, route in routes.items()
                                    if route["request_budget_mechanism_sha256"] is not None}
    if budgeted_routes and request_budget_controller is None:
        raise ExamError("frozen request-budget mechanism requires a request-budget controller")
    if request_budget_controller is not None:
        if not callable(getattr(request_budget_controller, "prepare", None)) or not callable(
            getattr(request_budget_controller, "bind", None)
        ) or not callable(getattr(request_budget_controller, "for_attempt", None)):
            raise ExamError("invalid request-budget controller")
        request_budget_controller.prepare(manifest, plan)
    if set(packets) != set(suites) or set(segment_plans) != set(suites) or set(configs) != set(routes):
        raise ExamError("research runtime inputs do not cover the frozen experiment")
    sources_urls = sources_urls or {}
    if set(sources_urls) - set(routes):
        raise ExamError("foreign Sources route")
    for suite_id, suite in suites.items():
        _validated_packet(packets[suite_id])
        validate_segment_plan(segment_plans[suite_id], packets[suite_id], suite_id=suite_id,
                              protocol_sha256=manifest["protocol_sha256"])
        if segment_plans[suite_id] != suite["segment_plan"]:
            raise ExamError("runtime segmentation differs from frozen manifest")
    for route_id, route in routes.items():
        if digest(configs[route_id]) != route["config_sha256"]:
            raise ExamError("runtime base configuration drift")
        if route_fingerprint(configs[route_id], sources_urls.get(route_id)) != route["route_sha256"]:
            raise ExamError("runtime endpoint drift")
    admission_probe.prepare(manifest, plan)
    if plan.get("spending_policy", {}).get("mode") == "sequential_shared_cap":
        validate_root = getattr(request_budget_controller, "validate_execution_root", None)
        if not callable(validate_root):
            raise ExamError("sequential spending controller cannot validate its execution root")
        validate_root(root)
    if root.is_symlink():
        raise ExamError("research directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=resume)
    with _lock(root):
        _immutable_json(root / "experiment.json", manifest)
        _immutable_json(root / "execution-plan.json", plan)
        if (root / "stop.json").exists():
            yield {"status": "stopped", "reason": read_json(root / "stop.json")["reason"]}
            return
        store = EvidenceStore(root / "evidence")
        existing = store.verify_all()
        admission_store = EvidenceStore(root / "admission-evidence")
        if request_budget_controller is not None:
            request_budget_controller.bind(root)
            budget_store = EvidenceStore(root / "request-budget-evidence")
        else:
            budget_store = None
        for probe_id, probe in admission_store.verify_all().items():
            if not probe["complete"]:
                admission_store.finalize(probe_id, {"schema": "ukrainian-llm-eval.admission-failure.v1",
                                                     "status": "failed", "reason": "interrupted"}, status="interrupted")
        scheduled = {segment["attempt_id"] for cell in plan["cells"] for segment in cell["segments"]}
        if set(existing) - scheduled:
            raise ExamError("foreign attempt in research evidence")
        sequential = plan.get("spending_policy", {}).get("mode") == "sequential_shared_cap"
        if sequential and not getattr(request_budget_controller, "uses_sequential_shared_cap", False):
            raise ExamError("sequential spending plan lacks its shared runtime ledger")
        # V1 admits the full frozen sum up front. V2 preserves that same full
        # schedule but atomically admits each segment against a shared ledger.
        expected_total = sum(segment["reserved_micro_usd"] for cell in plan["cells"]
                             for segment in cell["segments"])
        if expected_total != plan["reservation_total_micro_usd"] or (
            not sequential and expected_total > plan["new_spend_cap_micro_usd"]
        ):
            raise ExamError("research reservation ceiling mismatch")
        seen_sessions = set()
        cell_results = []
        for cell in plan["cells"]:
            suite_id, route_id = cell["suite_id"], cell["route_id"]
            suite, route = suites[suite_id], routes[route_id]
            packet, segmentation = packets[suite_id], segment_plans[suite_id]
            config = _research_config(configs[route_id], suite, manifest["repeats"])
            prepared = {item["segment_id"]: item for item in segmentation["segments"]}
            records, receipts, failure = [], [], None
            for scheduled_segment in cell["segments"]:
                segment_id = scheduled_segment["segment_id"]
                segment = derive_segment_packet(packet, prepared[segment_id]["item_ids"], segment_id=segment_id)
                context = {"execution_plan_sha256": plan["execution_plan_sha256"],
                           "cell_id": cell["cell_id"], "segment_id": segment_id,
                           "reservation_id": scheduled_segment["reservation_id"],
                           "reserved_micro_usd": scheduled_segment["reserved_micro_usd"]}
                attempt_id = scheduled_segment["attempt_id"]
                binding = {"execution_plan_sha256": plan["execution_plan_sha256"], "candidate_attempt_id": attempt_id,
                           "cell_id": cell["cell_id"], "segment_id": segment_id,
                           "segment_packet_sha256": segment["packet_sha256"], "config_sha256": digest(config),
                           "condition": cell["condition"]}
                composite = admission_composite_sha256(manifest, route, segmentation)
                expected_metadata = {"denominator": len(segment["items"]),
                                     "packet_sha256": segment["packet_sha256"], "config_sha256": digest(config),
                                     "condition": cell["condition"], "route_sha256": route["route_sha256"],
                                     "segment_context": context}
                receipt = existing.get(attempt_id)
                if receipt is not None:
                    stored_context = receipt["metadata"].get("segment_context", {})
                    admission_id = stored_context.get("admission_attempt_id")
                    if not isinstance(admission_id, str):
                        raise ExamError("research receipt lacks admission evidence")
                    admission_evidence = EvidenceStore(root / "admission-evidence").verify(admission_id)
                    verify_admission_evidence(admission_evidence, binding, route, composite, scheduled_segment["reserved_micro_usd"])
                    context.update(admission_attempt_id=admission_id, admission_receipt_sha256=digest(admission_evidence))
                    if receipt["metadata"] != expected_metadata:
                        raise ExamError("research receipt binding mismatch")
                    if not receipt["complete"]:
                        result = _failure(segment, config, cell["condition"], TimeoutError())
                        result["failure_reason"] = "interrupted"
                        receipt = store.finalize(attempt_id, result, status="interrupted")
                else:
                    request_budget = None
                    try:
                        if route_fingerprint(config, sources_urls.get(route_id)) != route["route_sha256"]:
                            raise ExamError("runtime endpoint drift")
                        request = build_admission_request(route, config, cell["condition"],
                                                          input_utf8_bytes=len(build_prompt(segment, cell["condition"], max_tool_calls=config["max_tool_calls"]).encode()),
                                                          tool_policy_sha256=manifest["tool_policy_sha256"], composite_sha256=composite)
                        if sequential:
                            available = request_budget_controller.remaining_ceiling_micro_usd()
                            if scheduled_segment["reserved_micro_usd"] > available:
                                raise SpendingCapExceeded(
                                    "next reservation exceeds the authorized shared new-spend cap"
                                )
                        else:
                            # Full-schedule admission already reserves every slot;
                            # this remainder excludes all other frozen reservations.
                            available = plan["new_spend_cap_micro_usd"] - expected_total + scheduled_segment["reserved_micro_usd"]
                        _, admission_evidence = admission_probe(route, config, cell["condition"], request=request,
                                                                 execution_binding=binding,
                                                                 reserved_micro_usd=scheduled_segment["reserved_micro_usd"],
                                                                 remaining_ceiling_micro_usd=available,
                                                                 evidence_dir=root / "admission-evidence")
                        admission_id = admission_evidence["attempt_id"]
                        verified = EvidenceStore(root / "admission-evidence").verify(admission_id)
                        if verified != admission_evidence:
                            raise ExamError("admission evidence differs from saved receipt")
                        verify_admission_evidence(verified, binding, route, composite, scheduled_segment["reserved_micro_usd"])
                        context.update(admission_attempt_id=admission_id, admission_receipt_sha256=digest(verified))
                        if request_budget_controller is not None:
                            if sequential:
                                request_budget = request_budget_controller.for_attempt(
                                    route, config, attempt_id, verified["result"],
                                    reservation_id=scheduled_segment["reservation_id"],
                                    reservation_binding=binding,
                                )
                            else:
                                request_budget = request_budget_controller.for_attempt(
                                    route, config, attempt_id, verified["result"]
                                )
                        if route_id in budgeted_routes and request_budget is None:
                            raise ExamError("candidate with required budgeting lacks a request-level budget")
                    except SpendingCapExceeded as exc:
                        remaining = []
                        found = False
                        for pending_cell in plan["cells"]:
                            pending_plan = segment_plans[pending_cell["suite_id"]]
                            segment_sizes = {
                                item["segment_id"]: len(item["item_ids"])
                                for item in pending_plan["segments"]
                            }
                            for pending in pending_cell["segments"]:
                                if pending["attempt_id"] == attempt_id:
                                    found = True
                                if found:
                                    remaining.append({
                                        "cell_id": pending_cell["cell_id"],
                                        "segment_id": pending["segment_id"],
                                        "attempt_id": pending["attempt_id"],
                                        "reservation_id": pending["reservation_id"],
                                        "denominator": segment_sizes[pending["segment_id"]],
                                        "status": "not_executed_budget",
                                    })
                        budget_stop = {
                            "schema": "ukrainian-llm-eval.research-budget-stop.v1",
                            "execution_plan_sha256": plan["execution_plan_sha256"],
                            "ledger_id": plan["spending_policy"]["ledger_id"],
                            "next_attempt_id": attempt_id,
                            "reason": "not_executed_budget",
                            "error_class": type(exc).__name__,
                            "remaining": remaining,
                            "remaining_denominator": sum(item["denominator"] for item in remaining),
                        }
                        _immutable_json(root / "budget-stop.json", budget_stop)
                        stop = {
                            "execution_plan_sha256": plan["execution_plan_sha256"],
                            "reason": "budget_exhausted",
                            "next_attempt_id": attempt_id,
                            "budget_stop_sha256": digest(budget_stop),
                        }
                        _immutable_json(root / "stop.json", stop)
                        yield {"status": "stopped", "reason": stop["reason"]}
                        return
                    except (ExamError, OSError, TimeoutError) as exc:
                        stop = {"execution_plan_sha256": plan["execution_plan_sha256"],
                                "reason": "admission_failed", "error_class": type(exc).__name__,
                                "next_attempt_id": attempt_id}
                        _immutable_json(root / "stop.json", stop)
                        yield {"status": "stopped", "reason": stop["reason"]}
                        return
                    result, receipt = execute_attempt(segment, config, cell["condition"], root / "evidence",
                                                       sources_url=sources_urls.get(route_id), attempt_id=attempt_id,
                                                       segment_context=context, request_budget=request_budget)
                result = receipt["result"]
                if route["request_budget_mechanism_sha256"] is not None:
                    if budget_store is None:
                        raise ExamError("request-budget evidence store is unavailable")
                    budget_evidence = budget_store.verify(request_budget_attempt_id(attempt_id))
                    verify_request_budget_evidence(budget_evidence, route, config, attempt_id, result)
                receipts.append(receipt)
                stop_reason = _research_stop_reason(result, route, config)
                observed_cost = _observed_micro_usd(result, route)
                if observed_cost is not None and observed_cost > scheduled_segment["reserved_micro_usd"]:
                    stop_reason = "cost_reservation_overrun"
                if stop_reason:
                    _immutable_json(root / "stop.json", {"execution_plan_sha256": plan["execution_plan_sha256"],
                                                        "reason": stop_reason, "attempt_id": attempt_id,
                                                        "receipt_sha256": digest(receipt)})
                    yield {"status": "stopped", "reason": stop_reason}
                    return
                candidate_failed = is_candidate_response_failure(
                    result, expected_response_ids=[item["id"] for item in segment["items"]]
                )
                if result["status"] != "ok" and not candidate_failed:
                    failure = result.get("failure_reason", "segment_failed")
                    break
                session_id = result.get("identity", {}).get("session_id")
                if not isinstance(session_id, str) or not session_id or session_id in seen_sessions:
                    _immutable_json(root / "stop.json", {"execution_plan_sha256": plan["execution_plan_sha256"],
                                                        "reason": "missing_or_reused_session", "attempt_id": attempt_id})
                    yield {"status": "stopped", "reason": "missing_or_reused_session"}
                    return
                seen_sessions.add(session_id)
                if candidate_failed:
                    # Preserve the failed cell under its frozen stop-cell
                    # policy, but continue later independently scheduled repeats.
                    failure = result["failure_reason"]
                    break
                from .adapters import AdapterError, _extract_responses

                try:
                    normalized = _extract_responses({"responses": result.get("responses")}, segment)
                    if result.get("packet_sha256") != segment["packet_sha256"]:
                        raise ExamError("segment packet identity drift")
                    if suite_id == "ua-gec-public-gec-only-test" and any(value is None for value in normalized.values()):
                        raise ExamError("GEC segment contains an incomplete correction")
                except (ExamError, AdapterError):
                    failure = "invalid_segment_response"
                    break
                records.append({"segment_id": segment_id, "packet_sha256": result["packet_sha256"],
                                "status": result["status"], "responses": normalized})
            # A failed cell may never have later attempts: they would contradict
            # the frozen stop-cell policy, even if their individual hashes verify.
            consumed_ids = {receipt["attempt_id"] for receipt in receipts}
            if any(item["attempt_id"] in existing and item["attempt_id"] not in consumed_ids
                   for item in cell["segments"]):
                raise ExamError("attempt exists after terminal cell failure")
            try:
                responses = reassemble_cell(segmentation, records) if failure is None else None
            except ExamError:
                failure, responses = "incomplete_or_invalid_cell", None
            result = {"schema": "ukrainian-llm-eval.research-cell.v1", "cell_id": cell["cell_id"],
                      "suite_id": suite_id, "route_id": route_id, "repeat": cell["repeat"],
                      "condition": cell["condition"], "packet_sha256": packet["packet_sha256"],
                      "segment_plan_sha256": segmentation["segment_plan_sha256"],
                      "execution_plan_sha256": plan["execution_plan_sha256"],
                      "status": "failed" if failure else "ok", "failure_reason": failure,
                      "responses": responses, "comparison": _comparison(packet, config),
                      "receipt_sha256s": [digest(receipt) for receipt in receipts],
                      "reserved_micro_usd_started": sum(receipt["metadata"]["segment_context"]["reserved_micro_usd"]
                                                        for receipt in receipts)}
            _immutable_json(root / (cell["cell_id"] + ".json"), result)
            cell_results.append(result)
            yield {"cell_id": cell["cell_id"], "status": result["status"],
                   "segments_completed": len(records), "segments_required": len(cell["segments"])}
        all_receipts = store.verify_all()
        admission_receipts = admission_store.verify_all()
        budget_receipts = {} if budget_store is None else budget_store.verify_all()
        scheduled_budget_ids = {
            request_budget_attempt_id(segment["attempt_id"])
            for cell in plan["cells"]
            if routes[cell["route_id"]]["request_budget_mechanism_sha256"] is not None
            for segment in cell["segments"]
        }
        if set(budget_receipts) - scheduled_budget_ids:
            raise ExamError("foreign attempt in request-budget evidence")
        ordered_receipts = [digest(all_receipts[segment["attempt_id"]]) for cell in plan["cells"]
                            for segment in cell["segments"] if segment["attempt_id"] in all_receipts]
        body = {"schema": "ukrainian-llm-eval.research-result-manifest.v1",
                "execution_plan_sha256": plan["execution_plan_sha256"],
                "receipt_set_sha256": digest(ordered_receipts),
                "cell_result_sha256s": [digest(result) for result in cell_results],
                "cells_required": len(plan["cells"]),
                "cells_complete": sum(result["status"] == "ok" for result in cell_results),
                "attempts_started": len(all_receipts), "new_spend_cap_micro_usd": plan["new_spend_cap_micro_usd"],
                "admission_attempts": len(admission_receipts),
                "admission_receipt_set_sha256": digest({key: digest(value) for key, value in admission_receipts.items()}),
                "request_budget_attempts": len(budget_receipts),
                "request_budget_receipt_set_sha256": digest(
                    {key: digest(value) for key, value in budget_receipts.items()}
                ),
                "reserved_micro_usd_started": sum(result["reserved_micro_usd_started"] for result in cell_results),
                "scorer_sha256": manifest["scorer_sha256"]}
        _immutable_json(root / "result-manifest.json", {**body, "result_manifest_sha256": digest(body)})
