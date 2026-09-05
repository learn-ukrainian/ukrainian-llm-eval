"""Pure deterministic segmentation for the primary benchmark suites.

The segment plan is a small frozen object which binds one source packet to a
protocol and an exact partition.  Segment packets keep the existing public
packet schemas and receive fresh local opaque ids; references and grading keys
never enter this module.  Candidate responses are mapped back to the source
ids only after every segment in a cell has succeeded.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping
from typing import Any

from .core import PACKET_SCHEMA, ExamError, _json_safe, digest, validate_packet
from .gec import GEC_PACKET_SCHEMA, _is_document_heading, _parse_m2, validate_gec_packet

SEGMENT_PLAN_SCHEMA = "ukrainian-llm-eval.segment-plan.v1"

_PLAN_FIELDS = {
    "schema",
    "protocol_sha256",
    "suite_id",
    "source_packet_sha256",
    "source_sha256",
    "denominator",
    "unit",
    "segments",
    "segment_plan_sha256",
}
_PLAN_BODY_FIELDS = tuple(sorted(_PLAN_FIELDS - {"segment_plan_sha256"}))
_SEGMENT_FIELDS = {"segment_id", "item_ids", "packet_sha256"}
_RESULT_FIELDS = {"segment_id", "packet_sha256", "status", "responses"}
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")

_NMT_SUITE = "nmt-2022-demo-ukrainian"
_ULP_SUITE = "ulp"
_GEC_SUITE = "ua-gec-public-gec-only-test"
_SUITES = frozenset({_NMT_SUITE, _ULP_SUITE, _GEC_SUITE})
_UNITS = {
    _NMT_SUITE: "nmt-task",
    _ULP_SUITE: "ulp-question",
    _GEC_SUITE: "ua-gec-document",
}
_DENOMINATOR_FIELDS = {
    _NMT_SUITE: {"items", "single_choice", "matching", "points"},
    _ULP_SUITE: {"items", "points"},
    _GEC_SUITE: {"sentences", "documents", "tokens"},
}


def _require_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExamError(f"{where} must be an object")
    return value


def _require_exact_dict(value: Any, expected: set[str], where: str) -> dict[str, Any]:
    result = _require_dict(value, where)
    if set(result) != expected:
        missing = sorted(expected - set(result))
        extra = sorted(set(result) - expected)
        detail: list[str] = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unknown {extra}")
        raise ExamError(f"{where} has invalid fields ({'; '.join(detail)})")
    return result


def _require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExamError(f"{where} must be a non-empty string")
    return value


def _require_digest(value: Any, where: str) -> str:
    text = _require_text(value, where)
    if _DIGEST_RE.fullmatch(text) is None:
        raise ExamError(f"{where} must be a lowercase SHA-256 hex digest")
    return text


def _validate_suite_id(value: Any, where: str = "suite_id") -> str:
    suite_id = _require_text(value, where)
    if suite_id not in _SUITES:
        raise ExamError(f"unsupported suite_id: {suite_id!r}")
    return suite_id


def _validate_packet(packet: Any) -> dict[str, Any]:
    value = _require_dict(packet, "packet")
    if value.get("schema") == GEC_PACKET_SCHEMA:
        validate_gec_packet(value)
    else:
        validate_packet(value)
    return value


def _packet_ids(packet: dict[str, Any]) -> list[str]:
    return [item["id"] for item in packet["items"]]


def _source_sha256(source: str) -> str:
    if not isinstance(source, str):
        raise ExamError("GEC source must be text")
    try:
        return hashlib.sha256(source.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise ExamError("GEC source must be valid UTF-8 text") from exc


def _gec_membership(
    source: str,
    packet_ids: list[str],
    packet_texts: list[str] | None = None,
) -> tuple[list[list[str]], int, str]:
    """Return source-order document membership without retaining source data."""

    source_hash = _source_sha256(source)
    try:
        blocks = _parse_m2(source)
    except (ExamError, TypeError, UnicodeError) as exc:
        raise ExamError(f"cannot derive GEC document membership: {exc}") from exc

    headings = [block["text"] for block in blocks if _is_document_heading(block)]
    if len(headings) != len(set(headings)):
        raise ExamError("GEC source contains duplicate document headings")

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] | None = None
    for block in blocks:
        if _is_document_heading(block):
            if current is not None and not current:
                raise ExamError("GEC source contains an empty document")
            current = []
            groups.append(current)
            continue
        if current is None:
            if headings:
                raise ExamError("GEC source contains content before its first document heading")
            current = []
            groups.append(current)
        current.append(block)
    if current is not None and not current:
        raise ExamError("GEC source contains an empty document")

    content_blocks = [block for group in groups for block in group]
    if len(content_blocks) != len(packet_ids):
        raise ExamError(
            "GEC source sentence count does not match packet "
            f"({len(content_blocks)} != {len(packet_ids)})"
        )
    if not content_blocks:
        raise ExamError("GEC source contains no scored sentences")
    if packet_texts is not None and [block["text"] for block in content_blocks] != packet_texts:
        raise ExamError("GEC source sentences do not match the source packet")

    item_groups: list[list[str]] = []
    offset = 0
    for group in groups:
        item_groups.append(packet_ids[offset : offset + len(group)])
        offset += len(group)
    if [item_id for group in item_groups for item_id in group] != packet_ids:
        raise ExamError("GEC source membership does not cover packet order")
    # ``documents`` follows the existing GEC manifest convention: a source
    # without heading records has zero explicit headings, while its content is
    # still kept together as one deterministic candidate segment.
    return item_groups, len(headings), source_hash


def _validate_denominator_shape(denominator: Any, suite_id: str, where: str = "denominator") -> dict[str, int]:
    value = _require_exact_dict(denominator, _DENOMINATOR_FIELDS[suite_id], where)
    normalized: dict[str, int] = {}
    for field, count in value.items():
        if type(count) is not int or count < 0:
            raise ExamError(f"{where}.{field} must be a non-negative integer")
        normalized[field] = count
    if suite_id != _GEC_SUITE and normalized["items"] <= 0:
        raise ExamError(f"{where}.items must be positive")
    if suite_id == _GEC_SUITE and normalized["sentences"] <= 0:
        raise ExamError(f"{where}.sentences must be positive")
    return normalized


def _packet_denominator(packet: dict[str, Any], suite_id: str, document_count: int | None = None) -> dict[str, int]:
    items = packet["items"]
    if suite_id == _NMT_SUITE:
        single_choice = sum(item["kind"] == "single" for item in items)
        matching = sum(item["kind"] == "matching" for item in items)
        points = sum(1 if item["kind"] == "single" else len(item["rows"]) for item in items)
        return {
            "items": len(items),
            "single_choice": single_choice,
            "matching": matching,
            "points": points,
        }
    if suite_id == _ULP_SUITE:
        return {"items": len(items), "points": len(items)}
    result = {
        "sentences": len(items),
        "documents": document_count if document_count is not None else 0,
        "tokens": sum(len(item["text"].split()) for item in items),
    }
    return result


def _check_denominator(
    denominator: dict[str, int],
    packet: dict[str, Any],
    suite_id: str,
    *,
    document_count: int | None = None,
) -> None:
    expected = _packet_denominator(packet, suite_id, document_count)
    for field, expected_count in expected.items():
        if suite_id == _GEC_SUITE and field == "documents" and document_count is None:
            continue
        if denominator[field] != expected_count:
            raise ExamError(
                f"{suite_id} denominator.{field} does not match packet "
                f"({denominator[field]} != {expected_count})"
            )


def _derive_segment_packet(packet: dict[str, Any], item_ids: list[str]) -> dict[str, Any]:
    packet_ids = _packet_ids(packet)
    if not isinstance(item_ids, list) or not item_ids:
        raise ExamError("segment item_ids must be a non-empty list")
    if any(not isinstance(item_id, str) or not item_id for item_id in item_ids):
        raise ExamError("segment item_ids must contain non-empty strings")
    if len(set(item_ids)) != len(item_ids):
        raise ExamError("segment item_ids contain duplicates")
    positions: list[int] = []
    for item_id in item_ids:
        try:
            positions.append(packet_ids.index(item_id))
        except ValueError as exc:
            raise ExamError(f"segment contains foreign item id {item_id!r}") from exc
    if positions != sorted(positions):
        raise ExamError("segment item_ids must follow source packet order")

    source_items = {item["id"]: item for item in packet["items"]}
    segment_items: list[dict[str, Any]] = []
    for local_index, item_id in enumerate(item_ids, start=1):
        item = copy.deepcopy(source_items[item_id])
        item["id"] = f"q{local_index:04d}"
        segment_items.append(item)
    body = {"schema": packet["schema"], "items": segment_items}
    result = body | {"packet_sha256": digest(body)}
    if result["schema"] == GEC_PACKET_SCHEMA:
        validate_gec_packet(result)
    else:
        validate_packet(result)
    return result


def derive_segment_packet(packet: dict[str, Any], item_ids: list[str], *, segment_id: str | None = None) -> dict[str, Any]:
    """Derive one existing-schema, gold-free packet for source ``item_ids``.

    ``segment_id`` is accepted for callers that want to associate the result
    while logging, but it is deliberately not embedded in the packet schema.
    """

    _validate_packet(packet)
    if segment_id is not None:
        _require_text(segment_id, "segment_id")
    return _derive_segment_packet(packet, item_ids)


def _plan_body(plan: dict[str, Any]) -> dict[str, Any]:
    return {field: plan[field] for field in _PLAN_BODY_FIELDS}


def _validate_plan_without_packet(plan: Any) -> dict[str, Any]:
    value = _require_exact_dict(plan, _PLAN_FIELDS, "segment plan")
    if value["schema"] != SEGMENT_PLAN_SCHEMA:
        raise ExamError(f"unsupported segment plan schema: {value['schema']!r}")
    _require_digest(value["protocol_sha256"], "segment plan.protocol_sha256")
    suite_id = _validate_suite_id(value["suite_id"], "segment plan.suite_id")
    _require_digest(value["source_packet_sha256"], "segment plan.source_packet_sha256")
    source_sha256 = value["source_sha256"]
    if suite_id == _GEC_SUITE:
        _require_digest(source_sha256, "segment plan.source_sha256")
    elif source_sha256 is not None:
        raise ExamError("segment plan.source_sha256 must be null outside GEC")
    _validate_denominator_shape(value["denominator"], suite_id, "segment plan.denominator")
    if value["unit"] != _UNITS[suite_id]:
        raise ExamError("segment plan.unit does not match suite")
    segments = value["segments"]
    if not isinstance(segments, list) or not segments:
        raise ExamError("segment plan.segments must be a non-empty list")
    expected_item_ids: list[str] = []
    for index, raw_segment in enumerate(segments, start=1):
        segment = _require_exact_dict(raw_segment, _SEGMENT_FIELDS, f"segment plan.segments[{index - 1}]")
        expected_segment_id = f"seg-{index:04d}"
        if segment["segment_id"] != expected_segment_id:
            raise ExamError("segment plan segments must use canonical order and ids")
        item_ids = segment["item_ids"]
        if not isinstance(item_ids, list) or not item_ids:
            raise ExamError(f"segment plan.segments[{index - 1}].item_ids must be a non-empty list")
        if any(not isinstance(item_id, str) or not item_id for item_id in item_ids):
            raise ExamError("segment plan item_ids must contain non-empty strings")
        if len(set(item_ids)) != len(item_ids):
            raise ExamError("segment plan contains duplicate item ids")
        _require_digest(segment["packet_sha256"], f"segment plan.segments[{index - 1}].packet_sha256")
        expected_item_ids.extend(item_ids)
    if len(expected_item_ids) != len(set(expected_item_ids)):
        raise ExamError("segment plan contains duplicate item ids")
    if not expected_item_ids:
        raise ExamError("segment plan contains no item ids")
    canonical_item_ids = [f"q{index:04d}" for index in range(1, len(expected_item_ids) + 1)]
    if expected_item_ids != canonical_item_ids:
        raise ExamError("segment plan item ids must be the source packet's canonical order")
    expected_hash = digest(_plan_body(value))
    if _require_digest(value["segment_plan_sha256"], "segment plan.segment_plan_sha256") != expected_hash:
        raise ExamError("segment plan content hash mismatch")
    return value


def validate_segment_plan(
    plan: dict[str, Any],
    packet: dict[str, Any],
    *,
    suite_id: str | None = None,
    protocol_sha256: str | None = None,
    denominator: dict[str, int] | None = None,
    gec_source: str | None = None,
) -> None:
    """Validate a frozen plan against its exact source packet partition."""

    value = _validate_plan_without_packet(plan)
    checked_packet = _validate_packet(packet)
    plan_suite = value["suite_id"]
    if suite_id is not None and _validate_suite_id(suite_id) != plan_suite:
        raise ExamError("segment plan suite_id does not match expected suite")
    if protocol_sha256 is not None and _require_digest(protocol_sha256, "protocol_sha256") != value["protocol_sha256"]:
        raise ExamError("segment plan protocol hash does not match expected protocol")
    if value["source_packet_sha256"] != checked_packet["packet_sha256"]:
        raise ExamError("segment plan is bound to a different source packet")
    if plan_suite == _GEC_SUITE and checked_packet["schema"] != GEC_PACKET_SCHEMA:
        raise ExamError("GEC segment plan requires a GEC packet")
    if plan_suite != _GEC_SUITE and checked_packet["schema"] != PACKET_SCHEMA:
        raise ExamError("MCQ segment plan requires a core question packet")
    if plan_suite == _GEC_SUITE and gec_source is not None:
        groups, document_count, source_hash = _gec_membership(
            gec_source,
            _packet_ids(checked_packet),
            [item["text"] for item in checked_packet["items"]],
        )
        if value["source_sha256"] != source_hash:
            raise ExamError("segment plan source hash does not match GEC source")
        expected_groups = [list(group) for group in groups]
        actual_groups = [list(segment["item_ids"]) for segment in value["segments"]]
        if actual_groups != expected_groups:
            raise ExamError("segment plan GEC document membership/order mismatch")
    elif plan_suite == _GEC_SUITE and gec_source is None:
        document_count = None
    elif gec_source is not None:
        raise ExamError("GEC source is only valid for a GEC segment plan")
    else:
        document_count = None
    expected_item_ids = _packet_ids(checked_packet)
    actual_item_ids = [item_id for segment in value["segments"] for item_id in segment["item_ids"]]
    if actual_item_ids != expected_item_ids:
        raise ExamError("segment plan must be an exact ordered disjoint packet partition")
    checked_denominator = _validate_denominator_shape(value["denominator"], plan_suite, "segment plan.denominator")
    if denominator is not None:
        expected_denominator = _validate_denominator_shape(denominator, plan_suite, "denominator")
        if expected_denominator != checked_denominator:
            raise ExamError("segment plan denominator does not match expected denominator")
    _check_denominator(checked_denominator, checked_packet, plan_suite, document_count=document_count)
    for index, segment in enumerate(value["segments"], start=1):
        derived = _derive_segment_packet(checked_packet, list(segment["item_ids"]))
        if segment["packet_sha256"] != derived["packet_sha256"]:
            raise ExamError(f"segment plan.segments[{index - 1}] packet hash mismatch")


def derive_segment_plan(
    packet: dict[str, Any],
    *,
    suite_id: str,
    protocol_sha256: str,
    denominator: dict[str, int],
    gec_source: str | None = None,
) -> dict[str, Any]:
    """Build the deterministic, gold-free segment plan for one suite."""

    checked_packet = _validate_packet(packet)
    checked_suite = _validate_suite_id(suite_id)
    checked_protocol = _require_digest(protocol_sha256, "protocol_sha256")
    checked_denominator = _validate_denominator_shape(denominator, checked_suite)
    packet_ids = _packet_ids(checked_packet)
    if checked_suite == _GEC_SUITE:
        if gec_source is None:
            raise ExamError("GEC segment planning requires the original M2 source")
        groups, document_count, source_sha256 = _gec_membership(
            gec_source,
            packet_ids,
            [item["text"] for item in checked_packet["items"]],
        )
        if checked_packet["schema"] != GEC_PACKET_SCHEMA:
            raise ExamError("GEC segment plan requires a GEC packet")
    else:
        if gec_source is not None:
            raise ExamError("GEC source is only valid for a GEC segment plan")
        groups = [[item_id] for item_id in packet_ids]
        document_count = None
        source_sha256 = None
        if checked_packet["schema"] != PACKET_SCHEMA:
            raise ExamError("MCQ segment plan requires a core question packet")
    _check_denominator(checked_denominator, checked_packet, checked_suite, document_count=document_count)

    segments: list[dict[str, Any]] = []
    for index, item_ids in enumerate(groups, start=1):
        segment_packet = _derive_segment_packet(checked_packet, list(item_ids))
        segments.append(
            {
                "segment_id": f"seg-{index:04d}",
                "item_ids": list(item_ids),
                "packet_sha256": segment_packet["packet_sha256"],
            }
        )
    body = {
        "schema": SEGMENT_PLAN_SCHEMA,
        "protocol_sha256": checked_protocol,
        "suite_id": checked_suite,
        "source_packet_sha256": checked_packet["packet_sha256"],
        "source_sha256": source_sha256,
        "denominator": checked_denominator,
        "unit": _UNITS[checked_suite],
        "segments": segments,
    }
    plan = body | {"segment_plan_sha256": digest(body)}
    validate_segment_plan(
        plan,
        checked_packet,
        suite_id=checked_suite,
        protocol_sha256=checked_protocol,
        denominator=checked_denominator,
        gec_source=gec_source,
    )
    return plan


def _result_records(segment_results: Any) -> list[dict[str, Any]]:
    if isinstance(segment_results, Mapping):
        records: list[dict[str, Any]] = []
        for segment_id, raw_record in segment_results.items():
            if not isinstance(segment_id, str):
                raise ExamError("segment result mapping keys must be strings")
            record = _require_dict(raw_record, f"segment result {segment_id!r}")
            if "segment_id" not in record:
                record = {"segment_id": segment_id, **record}
            elif record["segment_id"] != segment_id:
                raise ExamError("segment result mapping key does not match segment_id")
            records.append(record)
        return records
    if not isinstance(segment_results, list) or not segment_results:
        raise ExamError("segment_results must be a non-empty ordered list or mapping")
    return [_require_dict(record, f"segment_results[{index}]") for index, record in enumerate(segment_results)]


def reassemble_cell(plan: dict[str, Any], segment_results: Any) -> dict[str, Any]:
    """Reassemble one complete cell into source-order responses.

    Segment response ids are local to their derived packet.  A cell is
    reassembled only when every frozen segment is present exactly once, has a
    successful status and contains a complete response map.  MCQ ``null`` is
    retained as a deliberate abstention; GEC corrections must be non-empty
    one-line strings.
    """

    value = _validate_plan_without_packet(plan)
    records = _result_records(segment_results)
    expected_segments = value["segments"]
    expected_segment_ids = [segment["segment_id"] for segment in expected_segments]
    actual_segment_ids = [record.get("segment_id") for record in records]
    if actual_segment_ids != expected_segment_ids:
        raise ExamError("segment results must be an exact ordered segment cover")

    reassembled: dict[str, Any] = {}
    for index, (segment, raw_record) in enumerate(zip(expected_segments, records, strict=True)):
        record = _require_exact_dict(raw_record, _RESULT_FIELDS, f"segment result {index}")
        if record["segment_id"] != segment["segment_id"]:
            raise ExamError("segment result id does not match frozen plan")
        if record["packet_sha256"] != segment["packet_sha256"]:
            raise ExamError("segment result is bound to a different segment packet")
        if record["status"] != "ok":
            raise ExamError("cell cannot be reassembled from a failed segment")
        responses = record["responses"]
        if not isinstance(responses, dict):
            raise ExamError("segment result.responses must be an object")
        expected_local_ids = [f"q{local_index:04d}" for local_index in range(1, len(segment["item_ids"]) + 1)]
        if set(responses) != set(expected_local_ids):
            raise ExamError("segment response ids must exactly cover the derived segment packet")
        for local_id, response in responses.items():
            if value["suite_id"] == _GEC_SUITE:
                if not isinstance(response, str) or not response.strip() or response.splitlines() != [response]:
                    raise ExamError("GEC segment responses must be complete one-line corrections")
            elif response is not None and not isinstance(response, (str, dict)):
                raise ExamError("MCQ segment responses must be strings, objects, or null")
            if isinstance(response, dict):
                _json_safe(response, where=f"segment result.responses.{local_id}")
        for local_index, source_id in enumerate(segment["item_ids"], start=1):
            reassembled[source_id] = responses[f"q{local_index:04d}"]
    return reassembled


def reassemble_responses(plan: dict[str, Any], segment_results: Any) -> dict[str, Any]:
    """Compatibility alias for callers naming the returned object directly."""

    return reassemble_cell(plan, segment_results)


__all__ = [
    "SEGMENT_PLAN_SCHEMA",
    "derive_segment_packet",
    "derive_segment_plan",
    "reassemble_cell",
    "reassemble_responses",
    "validate_segment_plan",
]
