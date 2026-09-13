"""Ephemeral admission observations; no candidate execution or budget allocation."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .adapters import AdapterError, _condition_policy, build_prompt
from .admission import admission_composite_sha256, build_admission_request, verify_admission_evidence
from .core import ExamError, digest, read_json
from .evidence import EvidenceStore
from .execution import route_fingerprint
from .scheduling import _research_config, validate_research_runtime
from .segmentation import derive_segment_packet


def _execution_snapshot(root, manifest, plan):
    """Hash state without EvidenceStore creation or pending-final recovery."""
    if root.is_symlink():
        raise ExamError("execution root must not be a symlink")
    if not root.exists():
        return digest({"exists": False})
    if not root.is_dir():
        raise ExamError("execution root must be a directory")
    for name, expected in (("experiment.json", manifest), ("execution-plan.json", plan)):
        path = root / name
        if path.is_symlink():
            raise ExamError("execution artifact must not be a symlink")
        if path.exists() and read_json(path) != expected:
            raise ExamError("existing research artifact differs from frozen inputs")
    if (root / "stop.json").exists():
        raise ExamError("execution root has a retained stop; readiness cannot reset it")
    attempts = root / "evidence" / "attempts"
    scheduled = {segment["attempt_id"] for cell in plan["cells"] for segment in cell["segments"]}
    if attempts.exists() and {path.name for path in attempts.iterdir()} - scheduled:
        raise ExamError("foreign attempt in research evidence")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ExamError("execution evidence must not contain symlinks")
        if path.is_file():
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            files[str(path.relative_to(root))] = "directory"
        else:
            raise ExamError("execution evidence contains a special file")
    return digest({"exists": True, "entries": files})


def _condition(config, condition, sources_url):
    try:
        if config["adapter"] == "codex":
            if config.get("codex_tool_policy") == "reference-only":
                from .codex_reference import condition_policy

                condition_policy(config, condition, sources_url)
            else:
                from .native_codex import _validate_condition

                _validate_condition(condition, sources_url)
        elif config["adapter"] == "kimi":
            from .native_kimi import _validate_condition

            _validate_condition(config, condition, sources_url)
        elif config["adapter"] == "cursor":
            from .native_cursor import _validate_condition

            _validate_condition(condition, sources_url, config.get("tools") or [])
        else:
            _condition_policy(config, condition, sources_url)
    except AdapterError as exc:
        raise ExamError(str(exc)) from exc


def _sources_url_for_condition(sources_urls, route_id, condition):
    """Route maps bind Sources identity; only sources cells may receive the URL."""

    if condition == "sources":
        return sources_urls.get(route_id)
    return None


def check_research(packets, segment_plans, manifest, plan, configs, root: Path, *,
                   evidence_root: Path, admission_probe, request_budget_controller=None, sources_urls=None):
    """Observe one largest-byte representative per cell, validate every segment.

    Planned IDs serve only as bindings. No execution IDs or reservations are
    allocated. Only safe counts and identities reach the admission command.
    Bytes describe a size profile, not proof of provider token fit.
    """
    sources_urls = sources_urls or {}
    suites, routes, _ = validate_research_runtime(
        packets, segment_plans, manifest, plan, configs, root,
        admission_probe=admission_probe, request_budget_controller=request_budget_controller,
        sources_urls=sources_urls,
    )
    if evidence_root.is_symlink() or evidence_root.exists():
        raise ExamError("readiness requires a new observation directory")
    if (evidence_root.resolve().is_relative_to(root.resolve())
            or root.resolve().is_relative_to(evidence_root.resolve())):
        raise ExamError("readiness and execution directories must be separate")
    before = _execution_snapshot(root, manifest, plan)
    budget = None
    if request_budget_controller is not None:
        budget = request_budget_controller.inspect_readiness(root)
    sequential = plan.get("spending_policy", {}).get("mode") == "sequential_shared_cap"
    available = (budget["remaining_new_spend_micro_usd"] if sequential else
                 plan["new_spend_cap_micro_usd"] - plan["reservation_total_micro_usd"])
    prepared = []
    for cell in plan["cells"]:
        route, suite = routes[cell["route_id"]], suites[cell["suite_id"]]
        config = _research_config(configs[cell["route_id"]], suite, manifest["repeats"])
        route_sources = sources_urls.get(cell["route_id"])
        _condition(config, cell["condition"], _sources_url_for_condition(sources_urls, cell["route_id"], cell["condition"]))
        if route_fingerprint(config, route_sources) != route["route_sha256"]:
            raise ExamError("runtime endpoint drift")
        segmentation = segment_plans[cell["suite_id"]]
        segments = {item["segment_id"]: item for item in segmentation["segments"]}
        bindings, largest = [], None
        for scheduled in cell["segments"]:
            segment_id = scheduled["segment_id"]
            segment = derive_segment_packet(packets[cell["suite_id"]], segments[segment_id]["item_ids"],
                                            segment_id=segment_id)
            binding = {"execution_plan_sha256": plan["execution_plan_sha256"],
                       "candidate_attempt_id": scheduled["attempt_id"], "cell_id": cell["cell_id"],
                       "segment_id": segment_id, "segment_packet_sha256": segment["packet_sha256"],
                       "config_sha256": digest(config), "condition": cell["condition"]}
            size = len(build_prompt(segment, cell["condition"], max_tool_calls=config["max_tool_calls"]).encode())
            bindings.append(binding)
            if largest is None or size > largest[0]:
                largest = (size, binding, scheduled["reserved_micro_usd"])
        if largest is None:
            raise ExamError("readiness cell has no segments")
        prepared.append((cell, route, config, segmentation, bindings, largest))
    evidence_root.mkdir(mode=0o700, parents=True)
    observation = EvidenceStore(evidence_root / "observations").start({
        "denominator": len(prepared), "execution_plan_sha256": plan["execution_plan_sha256"],
        "manifest_sha256": digest(manifest), "execution_state_sha256": before,
        "budget_state_sha256": digest(budget), "purpose": "admission-only",
    })
    observation.append("budget_snapshot", {"snapshot": budget})
    probe_store = EvidenceStore(evidence_root / "admission-evidence")
    failures = 0
    for cell, route, config, segmentation, bindings, largest in prepared:
        size, binding, reserved = largest
        composite = admission_composite_sha256(manifest, route, segmentation)
        request = build_admission_request(route, config, cell["condition"], input_utf8_bytes=size,
                                          tool_policy_sha256=manifest["tool_policy_sha256"],
                                          composite_sha256=composite)
        record = {"cell_id": cell["cell_id"], "structural_segments": len(bindings),
                  "bindings_sha256": digest(bindings), "request_sha256": request["request_sha256"],
                  "representative_binding": binding, "input_utf8_bytes": size}
        try:
            _, evidence = admission_probe(
                route, config, cell["condition"], request=request, execution_binding=binding,
                reserved_micro_usd=reserved, remaining_ceiling_micro_usd=available if sequential else available + reserved,
                evidence_dir=evidence_root / "admission-evidence",
            )
            verified = probe_store.verify(evidence["attempt_id"])
            if verified != evidence or verified["metadata"].get("request_sha256") != request["request_sha256"]:
                raise ExamError("admission evidence differs from saved receipt")
            verify_admission_evidence(verified, binding, route, composite, reserved)
            if request_budget_controller is not None:
                request_budget_controller.check_readiness_capacity(route, verified["result"], budget)
            record.update(status="ok", admission_attempt_id=evidence["attempt_id"],
                          admission_receipt_sha256=digest(verified))
        except (ValueError, OSError) as exc:
            failures += 1
            record.update(status="failed", error_class=type(exc).__name__)
        observation.append("admission_observation", record)
    try:
        if _execution_snapshot(root, manifest, plan) != before:
            raise ExamError("execution state changed during observation")
        if request_budget_controller is not None and request_budget_controller.inspect_readiness(root) != budget:
            raise ExamError("budget state changed during observation")
    except (ValueError, OSError) as exc:
        failures += 1
        observation.append("state_changed", {"status": "failed", "error_class": type(exc).__name__})
    receipts = probe_store.verify_all()
    result = {"status": "failed" if failures else "ok", "execution_admitted": False,
              "cells": len(prepared), "structural_segments": sum(len(item[4]) for item in prepared),
              "failed_checks": failures, "representative_probes": len(receipts),
              "admission_receipt_set_sha256": digest({key: digest(value) for key, value in receipts.items()}),
              "refresh_required_at_execution": True}
    evidence = observation.finalize(result, status="failed" if failures else "completed")
    return {**result, "observation_attempt_id": evidence["attempt_id"], "observation_sha256": digest(evidence)}
