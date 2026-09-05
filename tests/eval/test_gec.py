from __future__ import annotations

import copy
import hashlib

import pytest

from ukrainian_llm_eval.core import ExamError, digest
from ukrainian_llm_eval.gec import (
    GEC_KEY_SCHEMA,
    GEC_PACKET_SCHEMA,
    prepare_gec,
    validate_gec_key,
    validate_gec_packet,
)

_PROVENANCE = {
    "source_url": "https://example.test/ua-gec",
    "source_revision": "synthetic-r1",
    "license": "test-only",
    "exposure": "synthetic",
}


def _m2() -> str:
    return (
        "S # 0001\n"
        "A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||0\n"
        "A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n"
        "\n"
        "S Це слово .\n"
        "A 1 1|||Spelling|||нове|||REQUIRED|||-NONE-|||0\n"
        "A 1 2|||Spelling|||слово|||REQUIRED|||-NONE-|||1\n"
        "\n"
        "S Це зайве слово .\n"
        "A 1 2|||Punctuation||||||REQUIRED|||-NONE-|||0\n"
        "A 1 2|||Punctuation||||||REQUIRED|||-NONE-|||1\n"
        "\n"
        "S Без помилок .\n"
        "A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||0\n"
        "A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n"
        "\n"
    )


def test_prepare_gec_keeps_source_packet_separate_from_all_references() -> None:
    packet, key = prepare_gec(_m2(), _PROVENANCE)

    assert packet["schema"] == GEC_PACKET_SCHEMA
    assert [item["id"] for item in packet["items"]] == ["q0001", "q0002", "q0003"]
    assert [item["text"] for item in packet["items"]] == ["Це слово .", "Це зайве слово .", "Без помилок ."]
    assert set(packet) == {"schema", "items", "packet_sha256"}
    assert all(set(item) == {"id", "text"} for item in packet["items"])
    assert "нове" not in str(packet)
    assert "replacement" not in str(packet)
    assert "annotations" not in str(packet)

    assert key["schema"] == GEC_KEY_SCHEMA
    assert key["source_sha256"] == hashlib.sha256(_m2().encode("utf-8")).hexdigest()
    assert key["provenance"] == _PROVENANCE
    assert [len(item["annotations"]) for item in key["items"]] == [2, 2, 2]
    assert key["items"][0]["annotations"][0]["start"] == 1
    assert key["items"][0]["annotations"][0]["end"] == 1
    assert key["items"][1]["annotations"][0]["replacement"] == ""
    assert key["items"][2]["annotations"][0]["start"] == -1
    assert key["items"][2]["annotations"][0]["end"] == -1
    assert key["items"][2]["annotations"][0]["replacement"] == "-NONE-"
    validate_gec_packet(packet)
    validate_gec_key(packet, key)


def test_only_verified_heading_shape_is_removed_from_numeric_and_hash_content() -> None:
    m2 = (
        "S # 0001\n"
        "A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||0\n"
        "A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n"
        "\n"
        "S 1234\n"
        "A 0 1|||Spelling|||5678|||REQUIRED|||-NONE-|||0\n"
        "A 0 1|||Spelling|||5678|||REQUIRED|||-NONE-|||1\n"
        "\n"
        "S # 1234\n"
        "A 0 2|||Spelling|||# 5678|||REQUIRED|||-NONE-|||0\n"
        "A 0 2|||Spelling|||# 5678|||REQUIRED|||-NONE-|||1\n"
        "\n"
        "S # заголовок\n"
        "A 0 2|||Spelling|||# текст|||REQUIRED|||-NONE-|||0\n"
        "A 0 2|||Spelling|||# текст|||REQUIRED|||-NONE-|||1\n"
        "\n"
    )

    packet, key = prepare_gec(m2, _PROVENANCE)

    assert [item["text"] for item in packet["items"]] == ["1234", "# 1234", "# заголовок"]
    assert len(key["items"]) == 3
    assert all(
        {annotation["annotator_id"] for annotation in item["annotations"]} == {"0", "1"}
        for item in key["items"]
    )


@pytest.mark.parametrize(
    "bad_m2",
    [
        "A 0 1|||Spelling|||x|||REQUIRED|||-NONE-|||0\n",
        "S text\nA 0 1|||Spelling|||x|||REQUIRED|||-NONE-|||0\n",
        "S text\nA 0 1|||Spelling|||x|||REQUIRED|||-NONE-|||0|||extra\n",
        "S text\nA 0|||Spelling|||x|||REQUIRED|||-NONE-|||0\n",
        "S text\nA -1 0|||Spelling|||x|||REQUIRED|||-NONE-|||0\nA -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n",
        "S text\nA 0 2|||Spelling|||x|||REQUIRED|||-NONE-|||0\nA -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n",
        "S text\nA -1 -1|||wrong|||x|||REQUIRED|||-NONE-|||0\nA -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n",
        "S text\nA -1 -1|||noop|||-NONE-|||OPTIONAL|||-NONE-|||0\nA -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n",
        "S text\nA -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||2\nA -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n",
        "S text\nA -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||0\nA -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||0\n",
    ],
)
def test_malformed_m2_is_rejected(bad_m2: str) -> None:
    with pytest.raises(ExamError):
        prepare_gec(bad_m2, _PROVENANCE)


def test_packet_tampering_and_key_rebinding_are_rejected() -> None:
    packet, key = prepare_gec(_m2(), _PROVENANCE)

    changed_packet = copy.deepcopy(packet)
    changed_packet["items"][0]["text"] = "Змінений текст ."
    with pytest.raises(ExamError, match="hash mismatch"):
        validate_gec_packet(changed_packet)

    rebound_key = copy.deepcopy(key)
    rebound_key["packet_sha256"] = "0" * 64
    with pytest.raises(ExamError, match="different packet"):
        validate_gec_key(packet, rebound_key)

    changed_key = copy.deepcopy(key)
    changed_key["items"][0]["annotations"][0]["replacement"] = "інше"
    with pytest.raises(ExamError, match="hash mismatch"):
        validate_gec_key(packet, changed_key)


def test_key_digest_is_bound_to_every_reference_record() -> None:
    _packet, key = prepare_gec(_m2(), _PROVENANCE)
    body = {field: key[field] for field in ("schema", "packet_sha256", "source_sha256", "provenance", "items")}
    assert key["key_sha256"] == digest(body)
