"""Prepare, run, score, and compare ZNO/NMT exams with separate key custody."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

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
from .runner import preflight, validate_config
from .scheduling import run_pair
from .typography import apply_typography
from .ulp import import_ulp, ulp_sidecar


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description=__doc__,
        epilog="Runtime artifacts are private. Only the export command emits an aggregate for sharing. "
        "See docs/running.md. Exit 0: success; 2: invalid input or failed trial.",
    )
    commands = cli.add_subparsers(dest="command", required=True)
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
    typography = commands.add_parser("apply-typography", help="Apply a source-bound, independently verified emphasis/headings overlay")
    for name in ("exam", "overlay", "output", "receipt"):
        typography.add_argument("--" + name, type=Path, required=True)
    gec = commands.add_parser("prepare-gec", help="Separate full UA-GEC M2 into source-only packet and private references")
    gec.add_argument("--input", type=Path, required=True)
    gec.add_argument("--provenance", type=Path, required=True)
    gec.add_argument("--questions", type=Path, required=True)
    gec.add_argument("--key", type=Path, required=True)
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


def execute(args: argparse.Namespace) -> int:
    if args.command == "evidence-status":
        if not args.evidence_dir.is_dir():
            raise ExamError("evidence directory must already exist")
        report = EvidenceStore(args.evidence_dir).inspect_all()
        write_private_json(args.output, report)
        complete = sum(item.get("complete") is True for item in report.values())
        corrupt = sum(item.get("status") == "corrupt" for item in report.values())
        print(json.dumps({"attempts": len(report), "complete": complete, "corrupt": corrupt}))
        return 0 if complete == len(report) else 2
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
        packet, key = prepare_gec(raw.decode("utf-8"), read_json(args.provenance))
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
