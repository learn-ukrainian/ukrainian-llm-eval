"""Offline scoring for sealed segmented research cells."""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

from .adapters import AdapterError, _extract_responses
from .admission import admission_composite_sha256, verify_admission_evidence
from .benchmark_manifest import validate_execution_plan
from .core import ExamError, digest, read_json, score_run
from .evidence import EvidenceStore
from .gec_scoring import score_gec_attempt
from .request_budget import request_budget_attempt_id, verify_request_budget_evidence
from .segmentation import derive_segment_packet, reassemble_cell, validate_segment_plan


def mcq_scorer_code_sha256() -> str:
    return hashlib.sha256(Path(__file__).with_name("core.py").read_bytes()).hexdigest()


def scorer_identity_sha256(bindings: dict[str, dict[str, str]]) -> str:
    if not isinstance(bindings, dict) or not bindings:
        raise ExamError("scorer bindings must be a non-empty object")
    for suite_id, binding in bindings.items():
        if not isinstance(suite_id, str) or not isinstance(binding, dict):
            raise ExamError("invalid scorer binding")
        if binding.get("kind") == "mcq-code":
            if set(binding) != {"kind", "code_sha256"} or binding["code_sha256"] != mcq_scorer_code_sha256():
                raise ExamError("MCQ scorer code identity drift")
        elif binding.get("kind") == "gec-image":
            image = binding.get("image_id")
            if set(binding) != {"kind", "image_id"} or not isinstance(image, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
                raise ExamError("invalid GEC scorer image identity")
        else:
            raise ExamError("unsupported scorer binding")
    return digest(bindings)


def _mean_sd(values: list[float]) -> dict[str, float]:
    mean = sum(values) / len(values)
    return {"mean": mean, "sample_sd": math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))}


def score_sealed_experiment(packets, segment_plans, keys, manifest, execution_plan, configs, execution_root: Path, *,
                            scorer_bindings, scoring_evidence_root: Path | None = None) -> dict[str, Any]:
    """Verify sealed receipts, score complete cells once, and summarize exact triples."""
    validate_execution_plan(manifest, execution_plan)
    if (execution_root / "stop.json").exists():
        raise ExamError("stopped experiment cannot emit a primary summary")
    result_manifest = read_json(execution_root / "result-manifest.json")
    if result_manifest.get("execution_plan_sha256") != execution_plan["execution_plan_sha256"] or result_manifest.get("scorer_sha256") != manifest["scorer_sha256"]:
        raise ExamError("result manifest differs from frozen scoring inputs")
    if result_manifest.get("result_manifest_sha256") != digest({k: v for k, v in result_manifest.items() if k != "result_manifest_sha256"}):
        raise ExamError("result manifest hash mismatch")
    if scorer_identity_sha256(scorer_bindings) != manifest["scorer_sha256"]:
        raise ExamError("scorer identity does not match frozen manifest")
    suites = {item["suite_id"]: item for item in manifest["suites"]}
    routes = {item["route_id"]: item for item in manifest["routes"]}
    if set(scorer_bindings) != set(suites):
        raise ExamError("scorer bindings must cover exactly the frozen suites")
    for suite_id in suites:
        expected_kind = "gec-image" if suite_id == "ua-gec-public-gec-only-test" else "mcq-code"
        if scorer_bindings[suite_id]["kind"] != expected_kind:
            raise ExamError("scorer kind does not match suite")
    if set(packets) != set(suites) or set(segment_plans) != set(suites) or set(keys) != set(suites) or set(configs) != set(routes):
        raise ExamError("scoring inputs do not cover frozen experiment")
    for suite_id, suite in suites.items():
        if digest(keys[suite_id]) != suite["key_sha256"]:
            raise ExamError("scoring key differs from frozen suite key")
        validate_segment_plan(segment_plans[suite_id], packets[suite_id], suite_id=suite_id,
                              protocol_sha256=manifest["protocol_sha256"])
        if segment_plans[suite_id] != suite["segment_plan"]:
            raise ExamError("scoring segment plan differs from frozen manifest")
    for route_id, route in routes.items():
        # Offline scoring authenticates the frozen fingerprint in receipts;
        # it must not resolve a live endpoint or need provider credentials.
        if digest(configs[route_id]) != route["config_sha256"]:
            raise ExamError("scoring route/config differs from frozen manifest")
    store = EvidenceStore(execution_root / "evidence")
    receipts = store.verify_all()
    admission_receipts = EvidenceStore(execution_root / "admission-evidence").verify_all()
    budget_receipts = EvidenceStore(execution_root / "request-budget-evidence").verify_all()
    if (result_manifest.get("request_budget_attempts") != len(budget_receipts)
            or result_manifest.get("request_budget_receipt_set_sha256") != digest(
                {key: digest(value) for key, value in budget_receipts.items()})):
        raise ExamError("result manifest request-budget set differs from evidence")
    if (result_manifest.get("admission_attempts") != len(admission_receipts)
            or result_manifest.get("admission_receipt_set_sha256") != digest(
                {key: digest(value) for key, value in admission_receipts.items()})):
        raise ExamError("result manifest admission set differs from evidence")
    scheduled = {s["attempt_id"] for c in execution_plan["cells"] for s in c["segments"]}
    if set(receipts) - scheduled:
        raise ExamError("foreign execution receipt")
    ordered = [digest(receipts[s["attempt_id"]]) for c in execution_plan["cells"] for s in c["segments"] if s["attempt_id"] in receipts]
    if result_manifest.get("receipt_set_sha256") != digest(ordered):
        raise ExamError("result manifest receipt set differs from evidence")
    artifacts = [read_json(execution_root / (cell["cell_id"] + ".json")) for cell in execution_plan["cells"]]
    if result_manifest.get("cell_result_sha256s") != [digest(artifact) for artifact in artifacts]:
        raise ExamError("result manifest cell hashes differ from artifacts")
    if result_manifest.get("cells_required") != len(execution_plan["cells"]) or result_manifest.get("attempts_started") != len(receipts):
        raise ExamError("result manifest denominator mismatch")
    cells = []
    for cell, artifact in zip(execution_plan["cells"], artifacts, strict=True):
        suite, route = suites[cell["suite_id"]], routes[cell["route_id"]]
        packet, plan = packets[cell["suite_id"]], segment_plans[cell["suite_id"]]
        artifact_identity = {**{field: cell[field] for field in ("cell_id", "suite_id", "route_id", "repeat", "condition")},
                             "packet_sha256": packet["packet_sha256"],
                             "segment_plan_sha256": plan["segment_plan_sha256"]}
        if any(artifact.get(field) != value for field, value in artifact_identity.items()):
            raise ExamError("sealed cell identity differs from frozen schedule")
        records, hashes, missing = [], [], False
        terminal_failure = False
        for segment in cell["segments"]:
            receipt = receipts.get(segment["attempt_id"])
            if receipt is None:
                missing = True; continue
            if missing or terminal_failure:
                raise ExamError("segment receipt exists after a missing or failed segment")
            segment_plan_item = next(item for item in plan["segments"] if item["segment_id"] == segment["segment_id"])
            derived_config = {**configs[cell["route_id"]], **suite["limits"], "repeats": manifest["repeats"]}
            expected = {"denominator": len(segment_plan_item["item_ids"]), "config_sha256": digest(derived_config),
                        "packet_sha256": segment["segment_packet_sha256"], "condition": cell["condition"],
                        "route_sha256": route["route_sha256"], "segment_context": {"execution_plan_sha256": execution_plan["execution_plan_sha256"], "cell_id": cell["cell_id"], "segment_id": segment["segment_id"], "reservation_id": segment["reservation_id"], "reserved_micro_usd": segment["reserved_micro_usd"]}}
            admission_id = receipt["metadata"].get("segment_context", {}).get("admission_attempt_id")
            if not isinstance(admission_id, str):
                raise ExamError("scoring segment lacks admission evidence")
            admission_evidence = admission_receipts.get(admission_id)
            if admission_evidence is None:
                raise ExamError("scoring segment admission evidence is missing")
            binding = {"execution_plan_sha256": execution_plan["execution_plan_sha256"], "candidate_attempt_id": segment["attempt_id"],
                       "cell_id": cell["cell_id"], "segment_id": segment["segment_id"],
                       "segment_packet_sha256": segment["segment_packet_sha256"], "config_sha256": digest(derived_config),
                       "condition": cell["condition"]}
            verify_admission_evidence(admission_evidence, binding, route, admission_composite_sha256(manifest, route, plan),
                                      segment["reserved_micro_usd"])
            expected["segment_context"].update(admission_attempt_id=admission_id, admission_receipt_sha256=digest(admission_evidence))
            if not receipt["complete"] or receipt["metadata"] != expected:
                raise ExamError("segment receipt binding mismatch")
            hashes.append(digest(receipt)); result = receipt["result"]
            if route["request_budget_mechanism_sha256"] is not None:
                budget_evidence = budget_receipts.get(request_budget_attempt_id(segment["attempt_id"]))
                if budget_evidence is None:
                    raise ExamError("scoring segment request-budget evidence is missing")
                verify_request_budget_evidence(budget_evidence, route, derived_config, segment["attempt_id"], result)
                if budget_evidence["metadata"]["account_sha256"] != admission_evidence["result"]["account_sha256"]:
                    raise ExamError("request-budget account differs from admission evidence")
            if result.get("status") == "ok" and receipt["terminal_status"] != "completed":
                raise ExamError("successful segment has non-completed evidence")
            if result.get("status") != "ok":
                terminal_failure = True
                continue
            derived_packet = derive_segment_packet(packet, segment_plan_item["item_ids"], segment_id=segment["segment_id"])
            try:
                normalized = _extract_responses({"responses": result.get("responses")}, derived_packet)
                if result.get("packet_sha256") != derived_packet["packet_sha256"]:
                    raise ExamError("segment packet identity drift")
                if cell["suite_id"] == "ua-gec-public-gec-only-test" and any(value is None for value in normalized.values()):
                    raise ExamError("incomplete GEC correction")
            except (ExamError, AdapterError):
                terminal_failure = True
                continue
            records.append({"segment_id": segment["segment_id"], "packet_sha256": result["packet_sha256"],
                            "status": "ok", "responses": normalized})
        if artifact.get("receipt_sha256s") != hashes or artifact.get("execution_plan_sha256") != execution_plan["execution_plan_sha256"]:
            raise ExamError("sealed cell artifact differs from receipts")
        if missing and not terminal_failure:
            raise ExamError("missing segment has no preceding failure evidence")
        if artifact.get("status") != ("failed" if terminal_failure else "ok"):
            raise ExamError("cell status contradicts verified segment outcomes")
        if terminal_failure:
            if artifact.get("responses") is not None:
                raise ExamError("failed cell contains a response aggregate")
            cells.append({"cell_id": cell["cell_id"], "suite_id": cell["suite_id"], "route_id": cell["route_id"], "repeat": cell["repeat"], "condition": cell["condition"], "status": "no_score"}); continue
        responses = reassemble_cell(plan, records)
        if artifact.get("responses") != responses or artifact.get("receipt_sha256s") != hashes or artifact.get("execution_plan_sha256") != execution_plan["execution_plan_sha256"]:
            raise ExamError("sealed cell artifact differs from receipts")
        if cell["suite_id"] == "ua-gec-public-gec-only-test":
            if scoring_evidence_root is None: raise ExamError("GEC scoring requires private scoring evidence root")
            if scoring_evidence_root.is_symlink():
                raise ExamError("scoring evidence root must not be a symlink")
            scoring_evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            aggregate = EvidenceStore(scoring_evidence_root / "aggregates")
            aid = "aggregate-" + digest(cell["cell_id"])[:48]
            if aid in aggregate.verify_all(): raise ExamError("derived aggregate already scored")
            metadata = {"denominator": len(packet["items"]), "derived_aggregate": True, "source_receipt_sha256s": hashes}
            run = {"schema": "ua-gec.run.v1", "packet_sha256": packet["packet_sha256"], "status": "ok", "responses": responses}
            aggregate.start(metadata, attempt_id=aid).finalize(run)
            score, _ = score_gec_attempt(packet, keys[cell["suite_id"]], scoring_evidence_root / "aggregates", aid,
                                         scorer_bindings[cell["suite_id"]]["image_id"], scoring_evidence_root / "gec-scores")
            value = score.get("metrics", {}).get("f0_5") if score.get("status") == "ok" else None
        else:
            run = {"schema": "zno-nmt.run.v1", "packet_sha256": packet["packet_sha256"], "condition": cell["condition"], "status": "ok", "responses": responses, "identity": {}, "comparison": {}, "metrics": {}}
            score = score_run(packet, keys[cell["suite_id"]], run); value = score["raw_points"]
        cells.append({"cell_id": cell["cell_id"], "suite_id": cell["suite_id"], "route_id": cell["route_id"], "repeat": cell["repeat"], "condition": cell["condition"], "status": "ok" if value is not None else "no_score", "value": value, "score": score})
    summaries = []
    for suite_id in suites:
        for route_id in routes:
            for condition in ("closed-book", "sources"):
                values = [c for c in cells if (c["suite_id"], c["route_id"], c["condition"]) == (suite_id, route_id, condition)]
                if {c["repeat"] for c in values if c["status"] == "ok"} == {1, 2, 3} and len(values) == 3:
                    summaries.append({"suite_id": suite_id, "route_id": route_id, "condition": condition, "values": [c["value"] for c in sorted(values, key=lambda c: c["repeat"])], **_mean_sd([c["value"] for c in values])})
    paired_deltas = []
    for suite_id in suites:
        for route_id in routes:
            grouped = {
                condition: {c["repeat"]: c["value"] for c in cells if c["suite_id"] == suite_id and c["route_id"] == route_id and c["condition"] == condition and c["status"] == "ok"}
                for condition in ("closed-book", "sources")
            }
            if set(grouped["closed-book"]) == set(grouped["sources"]) == {1, 2, 3}:
                values = [grouped["sources"][repeat] - grouped["closed-book"][repeat] for repeat in (1, 2, 3)]
                paired_deltas.append({"suite_id": suite_id, "route_id": route_id, "values": values, **_mean_sd(values)})
    return {"schema": "ukrainian-llm-eval.research-score-report.v1", "execution_plan_sha256": execution_plan["execution_plan_sha256"], "cells": cells, "summaries": summaries, "paired_deltas": paired_deltas}
