"""Strict UA-GEC M2 preparation with separate candidate and reference data.

This module only parses M2 and prepares custody-safe artifacts.  It does not
score corrections or apply an edit-distance approximation.  The public packet
contains source sentences and opaque ids; all annotations stay in the private
key.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any

from .core import ExamError, digest

GEC_PACKET_SCHEMA = "ua-gec.questions.v1"
GEC_KEY_SCHEMA = "ua-gec.key.v1"

_PROVENANCE_FIELDS = {"source_url", "source_revision", "license", "exposure"}
_PACKET_FIELDS = {"schema", "items", "packet_sha256"}
_PACKET_ITEM_FIELDS = {"id", "text"}
_KEY_FIELDS = {"schema", "packet_sha256", "source_sha256", "provenance", "items", "key_sha256"}
_KEY_BODY_FIELDS = ("schema", "packet_sha256", "source_sha256", "provenance", "items")
_KEY_ITEM_FIELDS = {"id", "annotations"}
_ANNOTATION_FIELDS = {
    "annotator_id",
    "start",
    "end",
    "category",
    "replacement",
    "required",
    "metadata",
}
_ANNOTATOR_IDS = frozenset({"0", "1"})
_INTEGER_TOKEN = re.compile(r"(?:0|[1-9][0-9]*|-[1-9][0-9]*)\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# ``str.splitlines`` treats all of these code points as line boundaries.  M2
# annotations are one physical line per record, so accepting any of them in a
# manually edited field would let a key change the serialized record layout.
_UNICODE_LINE_SEPARATORS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
# The pinned UA-GEC generator emits exactly ``S # ####`` for document
# headings in this split.  A numeric-only source sentence (``S 1234``) is
# ordinary content and must remain in the candidate packet.
_DOCUMENT_HEADING = re.compile(r"# [0-9]{4}\Z")


def _require_exact_dict(value: Any, expected: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExamError(f"{where} must be an object")
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        detail: list[str] = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unknown {extra}")
        raise ExamError(f"{where} has invalid fields ({'; '.join(detail)})")
    return value


def _required_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExamError(f"{where} must be a non-empty string")
    if _contains_line_separator(value):
        raise ExamError(f"{where} must not contain a Unicode line separator")
    return value


def _contains_line_separator(value: str) -> bool:
    """Return whether a string contains a Python/M2 record line boundary."""

    return any(character in _UNICODE_LINE_SEPARATORS for character in value)


def _required_token(value: Any, where: str) -> str:
    text = _required_text(value, where)
    if text != text.strip():
        raise ExamError(f"{where} must not have surrounding whitespace")
    return text


def _require_digest(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ExamError(f"{where} must be a lowercase SHA-256 hex digest")
    return value


def _validate_provenance(value: Any, where: str = "provenance") -> dict[str, str]:
    provenance = _require_exact_dict(value, _PROVENANCE_FIELDS, where)
    return {
        field: _required_text(provenance[field], f"{where}.{field}")
        for field in ("source_url", "source_revision", "license", "exposure")
    }


def _parse_integer_token(value: str, where: str) -> int:
    if not _INTEGER_TOKEN.fullmatch(value):
        raise ExamError(f"{where} must be a canonical integer")
    return int(value)


def _validate_annotation_semantics(annotation: dict[str, Any], token_count: int, where: str) -> None:
    start = annotation["start"]
    end = annotation["end"]
    category = annotation["category"]
    replacement = annotation["replacement"]
    if (start, end) == (-1, -1):
        if category != "noop" or replacement != "-NONE-":
            raise ExamError(f"{where} has an invalid no-op annotation")
        return
    if start < 0 or end < 0 or start > end or end > token_count:
        raise ExamError(f"{where} span {start}:{end} is outside the source token range")
    if category == "noop":
        raise ExamError(f"{where} no-op must use span -1:-1")
    if replacement == "-NONE-":
        raise ExamError(f"{where} uses the no-op replacement marker for a correction")
    if start == end and replacement == "":
        raise ExamError(f"{where} insertion must have a replacement")


def _validate_annotation_dict(value: Any, token_count: int, where: str) -> dict[str, Any]:
    annotation = _require_exact_dict(value, _ANNOTATION_FIELDS, where)
    annotator_id = _required_token(annotation["annotator_id"], f"{where}.annotator_id")
    if annotator_id not in _ANNOTATOR_IDS:
        raise ExamError(f"{where}.annotator_id must be one of 0 or 1")
    start = annotation["start"]
    end = annotation["end"]
    if type(start) is not int or type(end) is not int:
        raise ExamError(f"{where}.start and end must be integers")
    category = _required_token(annotation["category"], f"{where}.category")
    replacement = annotation["replacement"]
    if not isinstance(replacement, str):
        raise ExamError(f"{where}.replacement must be a string")
    if _contains_line_separator(replacement):
        raise ExamError(f"{where}.replacement must not contain a Unicode line separator")
    required = _required_token(annotation["required"], f"{where}.required")
    metadata = _required_token(annotation["metadata"], f"{where}.metadata")
    if required != "REQUIRED" or metadata != "-NONE-":
        raise ExamError(f"{where} has an unknown M2 annotation layout")
    normalized = {
        "annotator_id": annotator_id,
        "start": start,
        "end": end,
        "category": category,
        "replacement": replacement,
        "required": required,
        "metadata": metadata,
    }
    _validate_annotation_semantics(normalized, token_count, where)
    return normalized


def _validate_annotation_group(annotations: Any, source_text: str, where: str) -> list[dict[str, Any]]:
    if not isinstance(annotations, list) or not annotations:
        raise ExamError(f"{where} must contain annotations")
    token_count = len(source_text.split())
    normalized: list[dict[str, Any]] = []
    seen_by_annotator: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    for index, raw_annotation in enumerate(annotations):
        annotation = _validate_annotation_dict(raw_annotation, token_count, f"{where}[{index}]")
        signature = tuple(annotation[field] for field in (
            "start",
            "end",
            "category",
            "replacement",
            "required",
            "metadata",
        ))
        annotator_id = annotation["annotator_id"]
        if signature in seen_by_annotator[annotator_id]:
            raise ExamError(f"{where} contains a duplicate annotation for annotator {annotator_id}")
        seen_by_annotator[annotator_id].add(signature)
        normalized.append(annotation)

    annotator_ids = set(seen_by_annotator)
    if annotator_ids != _ANNOTATOR_IDS:
        missing = sorted(_ANNOTATOR_IDS - annotator_ids)
        unknown = sorted(annotator_ids - _ANNOTATOR_IDS)
        detail: list[str] = []
        if missing:
            detail.append(f"missing annotators {missing}")
        if unknown:
            detail.append(f"unknown annotators {unknown}")
        raise ExamError(f"{where} has invalid annotator IDs ({'; '.join(detail)})")

    for annotator_id in _ANNOTATOR_IDS:
        own_annotations = [annotation for annotation in normalized if annotation["annotator_id"] == annotator_id]
        noops = [annotation for annotation in own_annotations if annotation["start"] == -1]
        if noops and len(own_annotations) != 1:
            raise ExamError(f"{where} annotator {annotator_id} mixes a no-op with corrections")
    return normalized


def _parse_annotation(line: str, source_text: str, where: str) -> dict[str, Any]:
    fields = line[2:].split("|||", -1)
    if len(fields) != 6:
        raise ExamError(f"{where} must have six M2 fields")
    span_parts = fields[0].strip().split()
    if len(span_parts) != 2:
        raise ExamError(f"{where} has an invalid span field")
    start = _parse_integer_token(span_parts[0], f"{where}.start")
    end = _parse_integer_token(span_parts[1], f"{where}.end")
    annotation = {
        "annotator_id": fields[5],
        "start": start,
        "end": end,
        "category": fields[1],
        "replacement": fields[2],
        "required": fields[3],
        "metadata": fields[4],
    }
    return _validate_annotation_dict(annotation, len(source_text.split()), where)


def _is_document_heading(block: dict[str, Any]) -> bool:
    """Recognize the pinned generator's complete document-heading shape."""

    if not _DOCUMENT_HEADING.fullmatch(block["text"]):
        return False
    annotations = block["annotations"]
    return len(annotations) == 2 and {
        annotation["annotator_id"] for annotation in annotations
    } == _ANNOTATOR_IDS and all(
        annotation["start"] == -1
        and annotation["end"] == -1
        and annotation["category"] == "noop"
        and annotation["replacement"] == "-NONE-"
        and annotation["required"] == "REQUIRED"
        and annotation["metadata"] == "-NONE-"
        for annotation in annotations
    )


def _parse_m2(m2_text: str) -> list[dict[str, Any]]:
    if not isinstance(m2_text, str):
        raise ExamError("M2 input must be text")
    if not m2_text.strip():
        raise ExamError("M2 input must contain at least one sentence")

    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line_number, line in enumerate(m2_text.splitlines(), start=1):
        where = f"M2 line {line_number}"
        if line.startswith("S "):
            if current is not None:
                raise ExamError(f"{where} starts a sentence before the previous block was closed")
            source_text = _required_text(line[2:], f"{where}.source")
            current = {"text": source_text, "annotations": []}
            continue
        if line.startswith("A "):
            if current is None:
                raise ExamError(f"{where} annotation has no sentence")
            current["annotations"].append(_parse_annotation(line, current["text"], where))
            continue
        if line == "":
            if current is None:
                raise ExamError(f"{where} has an unexpected blank line")
            current["annotations"] = _validate_annotation_group(
                current["annotations"], current["text"], f"M2 sentence {len(blocks) + 1}.annotations"
            )
            blocks.append(current)
            current = None
            continue
        if not line.strip():
            raise ExamError(f"{where} must use an empty separator line")
        raise ExamError(f"{where} has an unknown M2 record")

    if current is not None:
        current["annotations"] = _validate_annotation_group(
            current["annotations"], current["text"], f"M2 sentence {len(blocks) + 1}.annotations"
        )
        blocks.append(current)
    if not blocks:
        raise ExamError("M2 input must contain at least one sentence")

    return blocks


def _source_sha256(m2_text: str) -> str:
    try:
        source_bytes = m2_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ExamError("M2 input must be valid UTF-8 text") from exc
    return hashlib.sha256(source_bytes).hexdigest()


def validate_gec_packet(packet: Any) -> None:
    """Validate a public source-only UA-GEC packet and its content digest."""

    value = _require_exact_dict(packet, _PACKET_FIELDS, "GEC packet")
    if value["schema"] != GEC_PACKET_SCHEMA:
        raise ExamError(f"unsupported GEC packet schema: {value['schema']!r}")
    raw_items = value["items"]
    if not isinstance(raw_items, list) or not raw_items:
        raise ExamError("GEC packet.items must be a non-empty list")
    items: list[dict[str, str]] = []
    for index, raw_item in enumerate(raw_items):
        item = _require_exact_dict(raw_item, _PACKET_ITEM_FIELDS, f"GEC packet.items[{index}]")
        item_id = _required_token(item["id"], f"GEC packet.items[{index}].id")
        expected_id = f"q{index + 1:04d}"
        if item_id != expected_id:
            raise ExamError("GEC packet item ids must be consecutive opaque ids starting at q0001")
        text = _required_text(item["text"], f"GEC packet.items[{index}].text")
        if text.splitlines() != [text]:
            raise ExamError(f"GEC packet.items[{index}].text must contain one source sentence")
        items.append({"id": item_id, "text": text})
    expected_hash = digest({"schema": GEC_PACKET_SCHEMA, "items": items})
    if _require_digest(value["packet_sha256"], "GEC packet.packet_sha256") != expected_hash:
        raise ExamError("GEC packet content hash mismatch")


def validate_gec_key(packet: Any, key: Any) -> None:
    """Validate private UA-GEC references and their packet binding."""

    validate_gec_packet(packet)
    packet_value = packet
    value = _require_exact_dict(key, _KEY_FIELDS, "GEC key")
    if value["schema"] != GEC_KEY_SCHEMA:
        raise ExamError(f"unsupported GEC key schema: {value['schema']!r}")
    packet_sha256 = _require_digest(packet_value["packet_sha256"], "GEC packet.packet_sha256")
    if value["packet_sha256"] != packet_sha256:
        raise ExamError("GEC key is bound to a different packet")
    _require_digest(value["source_sha256"], "GEC key.source_sha256")
    _validate_provenance(value["provenance"], "GEC key.provenance")

    raw_items = value["items"]
    if not isinstance(raw_items, list) or len(raw_items) != len(packet_value["items"]):
        raise ExamError("GEC key.items must reference every packet sentence exactly once")
    for index, raw_item in enumerate(raw_items):
        item = _require_exact_dict(raw_item, _KEY_ITEM_FIELDS, f"GEC key.items[{index}]")
        expected_id = packet_value["items"][index]["id"]
        if item["id"] != expected_id:
            raise ExamError("GEC key.items must follow packet order and ids")
        _validate_annotation_group(
            item["annotations"], packet_value["items"][index]["text"], f"GEC key.items[{index}].annotations"
        )

    key_body = {field: value[field] for field in _KEY_BODY_FIELDS}
    if _require_digest(value["key_sha256"], "GEC key.key_sha256") != digest(key_body):
        raise ExamError("GEC key content hash mismatch")


def prepare_gec(m2_text: str, provenance: dict, *, expected_sentences: int | None = None,
                expected_documents: int | None = None) -> tuple[dict, dict]:
    """Prepare a source-only packet and private full-reference key from M2."""

    for name, value, minimum in (("sentences", expected_sentences, 1), ("documents", expected_documents, 0)):
        if value is not None and (type(value) is not int or value < minimum):
            raise ExamError(f"expected {name} must be an integer at least {minimum}")
    normalized_provenance = _validate_provenance(provenance)
    blocks = _parse_m2(m2_text)
    source_sha256 = _source_sha256(m2_text)
    headings = [block["text"] for block in blocks if _is_document_heading(block)]
    content_blocks = [block for block in blocks if not _is_document_heading(block)]
    if expected_documents is not None and (len(headings) != expected_documents or len(set(headings)) != len(headings)):
        raise ExamError("M2 document denominator mismatch or duplicate heading")
    if expected_sentences is not None and len(content_blocks) != expected_sentences:
        raise ExamError("M2 sentence denominator mismatch")
    if not content_blocks:
        raise ExamError("M2 input contains no scored source sentences")

    packet_items = [
        {"id": f"q{index:04d}", "text": block["text"]}
        for index, block in enumerate(content_blocks, start=1)
    ]
    packet_body = {"schema": GEC_PACKET_SCHEMA, "items": packet_items}
    packet = packet_body | {"packet_sha256": digest(packet_body)}
    key_items = [
        {"id": packet_item["id"], "annotations": [dict(annotation) for annotation in block["annotations"]]}
        for packet_item, block in zip(packet_items, content_blocks, strict=True)
    ]
    key_body = {
        "schema": GEC_KEY_SCHEMA,
        "packet_sha256": packet["packet_sha256"],
        "source_sha256": source_sha256,
        "provenance": normalized_provenance,
        "items": key_items,
    }
    key = key_body | {"key_sha256": digest(key_body)}
    validate_gec_packet(packet)
    validate_gec_key(packet, key)
    return packet, key
