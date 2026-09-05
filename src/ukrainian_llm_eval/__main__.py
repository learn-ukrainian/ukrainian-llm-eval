"""Prepare, run, score, and compare ZNO/NMT exams with separate key custody."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from .admission import CommandAdmissionController
from .benchmark_manifest import (
    build_execution_plan,
    build_experiment_manifest,
    validate_execution_plan,
    verify_benchmark,
)
from .core import (
    ExamError,
    _duplicate_rejecting_pairs,
    _reject_json_constant,
    compare_runs,
    digest,
    prepare_exam,
    read_json,
    score_run,
    validate_packet,
    write_private_json,
)
from .evidence import EvidenceStore
from .execution import execute_attempt
from .gec import prepare_gec, validate_gec_packet
from .gec_scoring import score_gec_attempt
from .importers import import_zno
from .request_budget import RequestBudgetController
from .research_scoring import score_sealed_experiment
from .runner import preflight, validate_config
from .scheduling import run_pair, run_research
from .segmentation import derive_segment_plan
from .typography import apply_typography
from .ulp import import_ulp, ulp_sidecar


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description=__doc__,
        epilog="Runtime artifacts are private. Only the export command emits an aggregate for sharing. "
        "See docs/running.md. Exit 0: success; 2: invalid input or failed trial.",
    )
    commands = cli.add_subparsers(dest="command", required=True)
    segment = commands.add_parser("prepare-segments", help="Freeze a complete task/question/document partition")
    for name in ("questions", "denominator", "output"):
        segment.add_argument("--" + name, type=Path, required=True)
    segment.add_argument("--suite", required=True)
    segment.add_argument("--protocol-sha256", required=True)
    segment.add_argument("--gec-source", type=Path, help="Original private M2; preparation only, never executor input")
    research_plan = commands.add_parser("plan-research", help="Freeze experiment and exact conservative reservations; no provider calls")
    for name in ("specification", "manifest", "execution-plan"):
        research_plan.add_argument("--" + name, type=Path, required=True)
    research_run = commands.add_parser(
        "run-research",
        help="Execute a frozen research plan with explicitly supplied runtime inputs and admission commands",
    )
    research_run.add_argument("--inputs", "--runtime-inputs", dest="inputs", type=Path, required=True,
                              help="JSON map of packet, segment-plan and route-config files; never includes keys")
    for name in ("manifest", "execution-plan", "execution-root"):
        research_run.add_argument("--" + name, type=Path, required=True)
    research_run.add_argument(
        "--admission-specs", "--admission-commands", dest="admission_specs", type=Path, required=True,
        help="JSON route map to trusted admission-command specification files",
    )
    research_run.add_argument(
        "--operator-authorizations", "--authorizations", dest="operator_authorizations", type=Path, required=True,
        help="JSON route map to separate operator authorization files",
    )
    research_run.add_argument(
        "--request-budgets", type=Path,
        help="Route map to frozen request-budget mechanisms and trusted local counter commands; required by paid/credit routes",
    )
    research_run.add_argument(
        "--sources-urls", "--sources-routes", dest="sources_urls", type=Path,
        help="Optional JSON route-to-URL map; values may be env:NAME references",
    )
    research_run.add_argument(
        "--sources-url-env", action="append", default=[], metavar="ROUTE=ENV",
        help="Resolve a Sources URL from an environment variable (repeat; ENV alone applies to every Sources route)",
    )
    research_run.add_argument("--resume", action="store_true",
                              help="Resume the frozen root without retrying started segments")
    research_score = commands.add_parser("score-research", help="Verify sealed full-cell evidence and score offline")
    for name in ("inputs", "manifest", "execution-plan", "execution-root", "scorer-bindings", "output"):
        research_score.add_argument("--" + name, type=Path, required=True)
    research_score.add_argument("--scoring-evidence-dir", type=Path, help="Required for isolated UA-GEC scoring")
    imp = commands.add_parser("import-zno", help="Import one whole NLPForUA/ZNO paper, rejecting unsupported tasks")
    imp.add_argument("--input", type=Path, required=True)
    imp.add_argument("--test-id", required=True)
    imp.add_argument("--metadata", type=Path, required=True)
    imp.add_argument("--output", type=Path, required=True)
    ulp = commands.add_parser("import-ulp", help="Import ULP JSONL with separate source/category receipt")
    ulp.add_argument("--input", type=Path, required=True)
    ulp.add_argument("--metadata", type=Path, required=True)
    ulp.add_argument("--output", type=Path, required=True)
    ulp.add_argument("--sidecar", type=Path, required=True)
    ulp.add_argument("--source-sha256", help="Reject input bytes that do not match this SHA-256")
    verifier = commands.add_parser("verify-benchmark", help="Reconstruct artifacts from a pinned source and supplied profile")
    for name in ("profiles", "source", "questions", "key", "output"):
        verifier.add_argument("--" + name, type=Path, required=True)
    verifier.add_argument("--suite", required=True)
    verifier.add_argument("--exam", type=Path)
    verifier.add_argument("--overlay", type=Path)
    verifier.add_argument("--profile-sha256", help="Require this canonical profile digest")
    typography = commands.add_parser("apply-typography", help="Apply a source-bound, independently verified emphasis/headings overlay")
    for name in ("exam", "overlay", "output", "receipt"):
        typography.add_argument("--" + name, type=Path, required=True)
    gec = commands.add_parser("prepare-gec", help="Separate full UA-GEC M2 into source-only packet and private references")
    gec.add_argument("--input", type=Path, required=True)
    gec.add_argument("--provenance", type=Path, required=True)
    gec.add_argument("--questions", type=Path, required=True)
    gec.add_argument("--key", type=Path, required=True)
    gec.add_argument("--expected-sentences", type=int)
    gec.add_argument("--expected-documents", type=int)
    gec.add_argument("--source-sha256", help="Reject input bytes that do not match this SHA-256")
    prep = commands.add_parser("prepare", help="Separate a normalized exam into question-only packet and key")
    prep.add_argument("--exam", type=Path, required=True)
    prep.add_argument("--questions", type=Path, required=True)
    prep.add_argument("--key", type=Path, required=True)
    for name in ("preflight", "run", "pair"):
        run = commands.add_parser(name, help={
            "preflight": "Check configuration and tool capabilities without generating answers",
            "run": "Run one fresh trial; never accepts a grading key",
            "pair": "Freeze a repeated paired plan and run fresh sessions; never accepts a grading key",
        }[name])
        run.add_argument("--config", type=Path, required=True)
        run.add_argument("--sources-url-env", default="SOURCES_MCP_URL")
        if name != "pair":
            run.add_argument("--condition", choices=["closed-book", "sources"], required=True)
        if name != "preflight":
            run.add_argument("--questions", type=Path, required=True)
            run.add_argument("--output" if name == "run" else "--out-dir", type=Path, required=True)
        if name == "pair":
            run.add_argument("--resume", action="store_true", help="Resume the frozen plan without retrying prior attempts")
        if name == "run":
            run.add_argument("--evidence-dir", type=Path, help="Private raw evidence directory; defaults beside output")
    for name in ("score", "compare"):
        scorer = commands.add_parser(name, help="Offline key-custodian scoring; no provider calls")
        scorer.add_argument("--questions", type=Path, required=True)
        scorer.add_argument("--key", type=Path, required=True)
        scorer.add_argument("--output", type=Path, required=True)
        if name == "score":
            scorer.add_argument("--run", type=Path, required=True)
        else:
            scorer.add_argument("--control", type=Path, required=True)
            scorer.add_argument("--treatment", type=Path, required=True)
    gec_score = commands.add_parser("score-gec", help="Score a preserved GEC attempt offline with an immutable scorer image")
    for name in ("questions", "key", "run-evidence-dir", "evidence-dir", "output"):
        gec_score.add_argument("--" + name, type=Path, required=True)
    gec_score.add_argument("--attempt-id", required=True)
    gec_score.add_argument("--scorer-image", required=True)
    gec_score.add_argument("--timeout", type=int, default=600)
    evidence_status = commands.add_parser("evidence-status", help="Inspect every private attempt without executing providers")
    evidence_status.add_argument("--evidence-dir", type=Path, required=True)
    evidence_status.add_argument("--output", type=Path, required=True)
    export = commands.add_parser("export", help="Emit allowlisted numeric aggregates only, without text, keys or logs")
    export.add_argument("--input", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    return cli


PUBLIC_NUMERIC_FIELDS = frozenset({
    "raw_points", "max_points", "correct_items", "missing_items", "invalid_items", "passed",
    "expected_items", "expected_points", "source_items", "supported_items", "attempted_items",
    "total_items", "item_count", "score_delta", "raw_points_delta", "wins", "losses", "ties",
    "control_points", "treatment_points", "elapsed_seconds", "cost_usd", "input_tokens", "output_tokens",
    "tool_calls", "point_delta", "delta_points", "total", "complete", "failed_items",
    "items", "points", "control", "treatment", "delta", "total_tokens",
    "denominator", "sentences", "missing_sentences", "tp", "fp", "fn", "precision", "recall", "f0_5",
})


def public_aggregate(report: dict) -> dict:
    """Deliberately excludes arbitrary strings, item IDs, traces, metadata and paths."""
    result = {key: value for key, value in report.items()
              if key in PUBLIC_NUMERIC_FIELDS and (value is None or type(value) in {int, float, bool})}
    for key in ("denominator", "metrics", "control", "treatment", "tool_calls", "elapsed_seconds", "cost_usd"):
        if isinstance(report.get(key), dict):
            result[key] = public_aggregate(report[key])
    return result


def _research_scoring_inputs(path):
    spec = read_json(path)
    fields = {"schema", "packets", "segment_plans", "keys", "configs"}
    if not isinstance(spec, dict) or set(spec) != fields or spec["schema"] != "ukrainian-llm-eval.research-scoring-inputs.v1":
        raise ExamError("invalid research scoring input map")
    loaded = {}
    for field in fields - {"schema"}:
        if not isinstance(spec[field], dict) or not spec[field]:
            raise ExamError("invalid research scoring file map")
        loaded[field] = {}
        for identifier, relative in spec[field].items():
            if not isinstance(identifier, str) or not isinstance(relative, str) or not relative:
                raise ExamError("invalid research scoring file reference")
            loaded[field][identifier] = read_json(path.parent / relative)
    return loaded


_RESEARCH_RUNTIME_INPUTS_SCHEMA = "ukrainian-llm-eval.research-runtime-inputs.v1"
_RESEARCH_ADMISSION_SPECS_SCHEMA = "ukrainian-llm-eval.research-admission-specs.v1"
_RESEARCH_AUTHORIZATIONS_SCHEMA = "ukrainian-llm-eval.research-operator-authorizations.v1"
_RESEARCH_REQUEST_BUDGETS_SCHEMA = "ukrainian-llm-eval.research-request-budgets.v1"
_RESEARCH_SOURCES_SCHEMA = "ukrainian-llm-eval.research-sources-inputs.v1"


def _research_path_map(value, base: Path, label: str):
    """Load a strict identifier-to-JSON-file map relative to its map file."""
    if not isinstance(value, dict) or not value:
        raise ExamError(f"invalid {label} file map")
    loaded = {}
    for identifier, relative in value.items():
        if not isinstance(identifier, str) or not identifier:
            raise ExamError(f"invalid {label} file-map identifier")
        if not isinstance(relative, str) or not relative:
            raise ExamError(f"invalid {label} file reference")
        loaded[identifier] = read_json(base / relative)
    return loaded


def _research_runtime_inputs(path: Path):
    """Load candidate-visible runtime inputs while rejecting grading keys."""
    spec = read_json(path)
    required = {"schema", "packets", "segment_plans", "configs"}
    optional = {"sources_urls"}
    if not isinstance(spec, dict) or set(spec) - required - optional or not required <= set(spec):
        raise ExamError("invalid research runtime input map")
    if spec["schema"] != _RESEARCH_RUNTIME_INPUTS_SCHEMA:
        raise ExamError("unsupported research runtime input map")
    loaded = {
        "packets": _research_path_map(spec["packets"], path.parent, "packet"),
        "segment_plans": _research_path_map(spec["segment_plans"], path.parent, "segment-plan"),
        "configs": _research_path_map(spec["configs"], path.parent, "configuration"),
    }
    if "sources_urls" in spec:
        loaded["sources_urls"] = _resolve_research_sources(spec["sources_urls"])
    else:
        loaded["sources_urls"] = {}
    return loaded


def _research_admission_map(path: Path, *, schema: str, field: str, label: str, allow_empty: bool = False):
    """Load route-specific trusted integration files beside their map."""
    spec = read_json(path)
    expected = {"schema", field}
    if not isinstance(spec, dict) or set(spec) != expected or spec["schema"] != schema:
        raise ExamError(f"invalid research {label} map")
    if allow_empty and spec[field] == {}:
        return {}
    return _research_path_map(spec[field], path.parent, label)


def _resolve_research_sources(spec):
    if not isinstance(spec, dict):
        raise ExamError("invalid research Sources route map")
    result = {}
    for route_id, value in spec.items():
        if not isinstance(route_id, str) or not route_id or not isinstance(value, str) or not value.strip():
            raise ExamError("invalid research Sources route")
        value = value.strip()
        if value.startswith("env:"):
            env_name = value[4:]
            if (not env_name or not env_name.isascii() or not env_name.replace("_", "a").isalnum()
                    or env_name[0].isdigit()):
                raise ExamError("invalid Sources environment name")
            endpoint = os.environ.get(env_name)
            if not endpoint:
                raise ExamError("Sources route environment variable is not set")
            result[route_id] = endpoint
        else:
            result[route_id] = value
    return result


def _research_sources_urls(path: Path | None):
    if path is None:
        return {}
    spec = read_json(path)
    if isinstance(spec, dict) and "schema" in spec:
        if spec.get("schema") != _RESEARCH_SOURCES_SCHEMA or set(spec) != {"schema", "urls"}:
            raise ExamError("invalid research Sources route map")
        spec = spec["urls"]
    return _resolve_research_sources(spec)


def _research_sources_from_env(assignments, routes):
    """Resolve ROUTE=ENV assignments without exposing endpoint values."""
    source_routes = {route["route_id"] for route in routes if "sources" in route["conditions"]}
    result = {}
    for assignment in assignments:
        if not isinstance(assignment, str) or not assignment:
            raise ExamError("invalid Sources environment assignment")
        if "=" in assignment:
            route_id, env_name = assignment.split("=", 1)
            if not route_id or not env_name:
                raise ExamError("invalid Sources environment assignment")
            route_ids = {route_id}
        else:
            env_name = assignment
            route_ids = source_routes
        if (not env_name.isascii() or not env_name.replace("_", "a").isalnum()
                or env_name[0].isdigit()):
            raise ExamError("invalid Sources environment name")
        endpoint = os.environ.get(env_name)
        if not endpoint:
            raise ExamError("Sources route environment variable is not set")
        for route_id in route_ids:
            if route_id in result and result[route_id] != endpoint:
                raise ExamError("duplicate Sources route assignment")
            result[route_id] = endpoint
    return result


def _merge_research_sources(*maps):
    result = {}
    for source_map in maps:
        for route_id, endpoint in source_map.items():
            if route_id in result and result[route_id] != endpoint:
                raise ExamError("conflicting Sources route assignment")
            result[route_id] = endpoint
    return result


def execute(args: argparse.Namespace) -> int:
    if args.command == "prepare-segments":
        if args.output.exists():
            raise ExamError("refusing to replace a segment plan")
        plan = derive_segment_plan(read_json(args.questions), suite_id=args.suite,
                                   protocol_sha256=args.protocol_sha256, denominator=read_json(args.denominator),
                                   gec_source=args.gec_source.read_text(encoding="utf-8") if args.gec_source else None)
        write_private_json(args.output, plan)
        print(json.dumps({"segments": len(plan["segments"]), "segment_plan_sha256": plan["segment_plan_sha256"]}))
    elif args.command == "plan-research":
        if args.manifest.resolve() == args.execution_plan.resolve() or args.manifest.exists() or args.execution_plan.exists():
            raise ExamError("research manifest and execution plan must be distinct new files")
        spec = read_json(args.specification)
        required = {"protocol_sha256", "suites", "routes", "scorer_sha256", "tool_policy_sha256"}
        if not isinstance(spec, dict) or not required <= set(spec) or set(spec) - required - {"repeats", "new_spend_cap_micro_usd"}:
            raise ExamError("invalid research specification fields")
        manifest = build_experiment_manifest(**spec)
        plan = build_execution_plan(manifest)
        write_private_json(args.manifest, manifest)
        write_private_json(args.execution_plan, plan)
        print(json.dumps({"cells": len(plan["cells"]), "reservation_total_micro_usd": plan["reservation_total_micro_usd"],
                          "experiment_manifest_sha256": manifest["experiment_manifest_sha256"],
                          "execution_plan_sha256": plan["execution_plan_sha256"], "execution_admitted": False}))
    elif args.command == "run-research":
        runtime = _research_runtime_inputs(args.inputs)
        manifest = read_json(args.manifest)
        plan = read_json(args.execution_plan)
        validate_execution_plan(manifest, plan)
        specs = _research_admission_map(
            args.admission_specs,
            schema=_RESEARCH_ADMISSION_SPECS_SCHEMA,
            field="routes",
            label="admission-command",
        )
        authorizations = _research_admission_map(
            args.operator_authorizations,
            schema=_RESEARCH_AUTHORIZATIONS_SCHEMA,
            field="routes",
            label="operator-authorization",
        )
        budget_specs = {}
        if args.request_budgets is not None:
            budget_specs = _research_admission_map(
                args.request_budgets,
                schema=_RESEARCH_REQUEST_BUDGETS_SCHEMA,
                field="routes",
                label="request-budget",
                allow_empty=True,
            )
        if (not isinstance(manifest, dict) or not isinstance(manifest.get("routes"), list)
                or any(not isinstance(route, dict) or not isinstance(route.get("route_id"), str)
                       for route in manifest["routes"])):
            raise ExamError("invalid research manifest")
        sources_urls = _merge_research_sources(
            runtime["sources_urls"],
            _research_sources_urls(args.sources_urls),
            _research_sources_from_env(args.sources_url_env, manifest["routes"]),
        )
        controller = CommandAdmissionController(specs, authorizations)
        budget_controller = RequestBudgetController(budget_specs)
        failed = False
        for progress in run_research(
            runtime["packets"], runtime["segment_plans"], manifest, plan, runtime["configs"],
            args.execution_root, admission_probe=controller, request_budget_controller=budget_controller,
            sources_urls=sources_urls, resume=args.resume,
        ):
            print(json.dumps(progress, ensure_ascii=False), flush=True)
            failed |= progress.get("status") != "ok"
        return 2 if failed else 0
    elif args.command == "score-research":
        if args.output.exists():
            raise ExamError("refusing to replace a research score report")
        values = _research_scoring_inputs(args.inputs)
        report = score_sealed_experiment(values["packets"], values["segment_plans"], values["keys"],
                                          read_json(args.manifest), read_json(args.execution_plan), values["configs"],
                                          args.execution_root, scorer_bindings=read_json(args.scorer_bindings),
                                          scoring_evidence_root=args.scoring_evidence_dir)
        write_private_json(args.output, report)
        complete = sum(cell["status"] == "ok" for cell in report["cells"])
        print(json.dumps({"cells": len(report["cells"]), "cells_scored": complete,
                          "complete_triples": len(report["summaries"]), "complete_pairs": len(report["paired_deltas"])}))
        return 0 if complete == len(report["cells"]) else 2
    elif args.command == "evidence-status":
        if not args.evidence_dir.is_dir():
            raise ExamError("evidence directory must already exist")
        report = EvidenceStore(args.evidence_dir).inspect_all()
        write_private_json(args.output, report)
        complete = sum(item.get("complete") is True for item in report.values())
        corrupt = sum(item.get("status") == "corrupt" for item in report.values())
        print(json.dumps({"attempts": len(report), "complete": complete, "corrupt": corrupt}))
        return 0 if complete == len(report) else 2
    elif args.command == "verify-benchmark":
        if args.output.exists():
            raise ExamError("refusing to replace a benchmark manifest")
        profiles = read_json(args.profiles)
        if not isinstance(profiles, dict) or profiles.get("schema") != "ukrainian-llm-eval.benchmark-sources.v1":
            raise ExamError("unsupported source profile collection")
        suites = profiles.get("suites")
        if not isinstance(suites, list) or any(not isinstance(item, dict) for item in suites):
            raise ExamError("invalid source profile collection")
        selected = [item for item in suites if item.get("id") == args.suite]
        if len(selected) != 1:
            raise ExamError("suite must select exactly one source profile")
        if args.profile_sha256 is not None and digest(selected[0]) != args.profile_sha256:
            raise ExamError("source profile digest mismatch")
        report = verify_benchmark(selected[0], args.source.read_bytes(), read_json(args.questions),
                                  read_json(args.key), exam=read_json(args.exam) if args.exam else None,
                                  overlay=read_json(args.overlay) if args.overlay else None)
        write_private_json(args.output, report)
        print(json.dumps({"verification": report["verification"], "profile_sha256": report["profile_sha256"],
                          "packet_sha256": report["packet_sha256"]}))
    elif args.command == "apply-typography":
        if args.output.resolve() == args.receipt.resolve() or args.output.exists() or args.receipt.exists():
            raise ExamError("typography output and receipt must be distinct new files")
        exam, receipt = apply_typography(read_json(args.exam), read_json(args.overlay))
        write_private_json(args.output, exam)
        write_private_json(args.receipt, receipt)
        print(json.dumps({"change_count": receipt["change_count"], "result_packet_sha256": receipt["result_packet_sha256"]}))
    elif args.command == "prepare-gec":
        if args.questions.resolve() == args.key.resolve() or args.questions.exists() or args.key.exists():
            raise ExamError("question and key destinations must be distinct new files")
        raw = args.input.read_bytes()
        if args.source_sha256 is not None and hashlib.sha256(raw).hexdigest() != args.source_sha256:
            raise ExamError("UA-GEC source hash mismatch")
        packet, key = prepare_gec(raw.decode("utf-8"), read_json(args.provenance),
                                  expected_sentences=args.expected_sentences, expected_documents=args.expected_documents)
        write_private_json(args.questions, packet)
        write_private_json(args.key, key)
        print(json.dumps({"prepared_sentences": len(packet["items"]), "packet_sha256": packet["packet_sha256"]}))
    elif args.command == "import-ulp":
        if args.output.resolve() == args.sidecar.resolve() or args.output.exists() or args.sidecar.exists():
            raise ExamError("exam and sidecar must be distinct new files")
        raw = args.input.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        if args.source_sha256 is not None and args.source_sha256 != source_hash:
            raise ExamError("ULP source hash mismatch")
        rows = [json.loads(line, object_pairs_hook=_duplicate_rejecting_pairs,
                           parse_constant=_reject_json_constant)
                for line in raw.decode("utf-8").splitlines() if line.strip()]
        exam = import_ulp(rows, read_json(args.metadata))
        packet, _key = prepare_exam(exam)
        sidecar = {**ulp_sidecar(rows), "source_sha256": source_hash,
                   "packet_sha256": packet["packet_sha256"], "exam_sha256": digest(exam)}
        write_private_json(args.output, exam)
        write_private_json(args.sidecar, sidecar)
        print(json.dumps({"imported_items": len(rows), "source_sha256": source_hash}))
    elif args.command == "import-zno":
        exam = import_zno(read_json(args.input), args.test_id, read_json(args.metadata))
        write_private_json(args.output, exam)
        print(json.dumps({"imported_items": len(exam["items"])}))
    elif args.command == "prepare":
        if args.questions.resolve() == args.key.resolve() or args.questions.exists() or args.key.exists():
            raise ExamError("question and key destinations must be distinct new files")
        packet, key = prepare_exam(read_json(args.exam))
        write_private_json(args.questions, packet)
        write_private_json(args.key, key)
        print(json.dumps({"prepared_items": len(packet["items"]), "packet_sha256": packet["packet_sha256"]}))
    elif args.command in {"preflight", "run", "pair"}:
        config = read_json(args.config)
        validate_config(config)
        endpoint = os.environ.get(args.sources_url_env)
        if args.command == "preflight":
            result = preflight(config, args.condition, endpoint)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        packet = read_json(args.questions)
        if packet.get("schema") == "ua-gec.questions.v1":
            validate_gec_packet(packet)
        else:
            validate_packet(packet)
        plan = {"schema": "zno-nmt.plan.v1", "packet_sha256": packet["packet_sha256"],
                "config": config, "config_sha256": digest(config)}
        if args.command == "run":
            if args.output.exists():
                raise ExamError("refusing to replace a trial")
            write_private_json(args.output.with_suffix(".plan.json"), {**plan, "condition": args.condition})
            result, receipt = execute_attempt(
                packet, config, args.condition,
                args.evidence_dir or args.output.with_suffix(".evidence"), sources_url=endpoint,
            )
            write_private_json(args.output.with_suffix(".evidence.json"), receipt)
            write_private_json(args.output, result)
            print(json.dumps({"condition": args.condition, "status": result["status"]}))
            return 0 if result["status"] == "ok" else 2
        failed = False
        for progress in run_pair(packet, config, args.out_dir, sources_url=endpoint, resume=args.resume):
            failed = progress.pop("failed")
            print(json.dumps(progress), flush=True)
        return 2 if failed else 0
    elif args.command == "score-gec":
        if args.output.exists() or args.output.with_suffix(".evidence.json").exists():
            raise ExamError("refusing to replace scoring output or receipt")
        report, receipt = score_gec_attempt(
            read_json(args.questions), read_json(args.key), args.run_evidence_dir,
            args.attempt_id, args.scorer_image, args.evidence_dir, timeout=args.timeout,
        )
        write_private_json(args.output.with_suffix(".evidence.json"), receipt)
        write_private_json(args.output, report)
        print(json.dumps({"status": report["status"], **public_aggregate(report)}))
        return 0 if report["status"] == "ok" else 2
    elif args.command in {"score", "compare"}:
        packet, key = read_json(args.questions), read_json(args.key)
        if args.command == "score":
            report = score_run(packet, key, read_json(args.run))
        else:
            report = compare_runs(packet, key, read_json(args.control), read_json(args.treatment))
        write_private_json(args.output, report)
        print(json.dumps(public_aggregate(report), ensure_ascii=False))
    else:
        report = {"schema": "zno-nmt.public-aggregate.v1", **public_aggregate(read_json(args.input))}
        write_private_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False))
    return 0


def main() -> int:
    args = parser().parse_args()
    try:
        return execute(args)
    except (ExamError, ValueError, OSError) as exc:
        # Even unexpected transport failures must not print a private endpoint or credential.
        print(json.dumps({"status": "failed", "error_class": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
