from __future__ import annotations

import copy
import hashlib
import json

import pytest

from ukrainian_llm_eval.benchmark_manifest import MANIFEST_SCHEMA, verify_benchmark
from ukrainian_llm_eval.core import ExamError, digest, prepare_exam
from ukrainian_llm_eval.gec import prepare_gec
from ukrainian_llm_eval.importers import import_zno
from ukrainian_llm_eval.typography import TYPOGRAPHY_SCHEMA, apply_typography
from ukrainian_llm_eval.ulp import import_ulp


def _nmt_metadata(profile: dict) -> dict:
    return {
        "title": "Synthetic NMT manifest fixture",
        "subject": "Ukrainian",
        "year": 2022,
        "provenance": {
            "source_url": "https://example.test/nmt",
            "source_revision": profile["revision"],
            "license": profile["license"],
            "exposure": "synthetic",
        },
        "scoring": {
            "kind": "benchmark",
            "policy_url": None,
            "pass_threshold": None,
            "expected_items": 2,
            "expected_points": 3,
        },
    }


def _nmt_source() -> list[dict]:
    return [
        {
            "test_id": "515",
            "num_tasks": 2,
            "tasks": [
                {
                    "task_id": 0,
                    "question": "Choose X.",
                    "answers": [{"answer": "A", "text": "X"}, {"answer": "B", "text": "Y"}],
                    "answer_vheader": ["A", "B"],
                    "answer_hheader": [],
                    "correct_answer": ["A"],
                    "with_photo": False,
                },
                {
                    "task_id": 1,
                    "question": "Match the rows.",
                    "answers": [
                        {"answer": "A", "text": "one"},
                        {"answer": "B", "text": "two"},
                        {"answer": "C", "text": "three"},
                    ],
                    "answer_vheader": ["A", "B", "C"],
                    "answer_hheader": ["1", "2"],
                    "correct_answer": ["A", "B"],
                    "with_photo": False,
                },
            ],
        }
    ]


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _nmt_fixture() -> tuple[dict, bytes, dict, dict, dict, dict]:
    raw = _json_bytes(_nmt_source())
    profile = {
        "id": "nmt-2022-demo-ukrainian",
        "revision": "synthetic-nmt-r1",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "license": "synthetic-license",
        "selection": {"test_id": "515"},
        "official_paper": {"url": "https://example.test/nmt-paper.pdf", "sha256": "a" * 64},
        "denominator": {"items": 2, "single_choice": 1, "matching": 1, "points": 3},
    }
    plain_exam = import_zno(_nmt_source(), "515", _nmt_metadata(profile))
    overlay = {
        "schema": TYPOGRAPHY_SCHEMA,
        "source_exam_sha256": digest(plain_exam),
        "official_source": copy.deepcopy(profile["official_paper"]),
        "changes": [
            {
                "item_id": "1",
                "field": "question",
                "original_text": "Choose",
                "replacement_text": "*Choose*",
            },
            {
                "item_id": "2",
                "field": "matching_headings",
                "left": "Left heading",
                "right": "Right heading",
            },
        ],
    }
    exam, _typography_receipt = apply_typography(plain_exam, overlay)
    packet, key = prepare_exam(exam)
    return profile, raw, exam, packet, key, overlay


def _ulp_row(*, question: str, answer: int, answer_letter: str, debug_id: str) -> dict:
    return {
        "question": question,
        "choices": ["one", "two", "three"],
        "answer": answer,
        "answer_letter": answer_letter,
        "debug_id": debug_id,
        "category": "grammar",
    }


def _ulp_fixture() -> tuple[dict, bytes, dict, dict, dict]:
    profile = {
        "id": "ulp",
        "revision": "synthetic-ulp-r1",
        "source_sha256": "0" * 64,
        "license": "synthetic-license",
        "source_path": "synthetic.jsonl",
        "denominator": {"items": 2, "points": 2},
    }
    rows = [
        _ulp_row(question="Question one.", answer=0, answer_letter="А", debug_id="one"),
        _ulp_row(question="Question two.", answer=1, answer_letter="Б", debug_id="two"),
    ]
    raw = b"".join(_json_bytes(row) + b"\n" for row in rows)
    profile["source_sha256"] = hashlib.sha256(raw).hexdigest()
    metadata = {
        "title": "Synthetic ULP manifest fixture",
        "subject": "Ukrainian",
        "year": 2024,
        "provenance": {
            "source_url": "https://example.test/ulp",
            "source_revision": profile["revision"],
            "license": profile["license"],
            "exposure": "synthetic",
        },
        "scoring": {
            "kind": "benchmark",
            "policy_url": None,
            "pass_threshold": None,
            "expected_items": 2,
            "expected_points": 2,
        },
    }
    exam = import_ulp(rows, metadata)
    packet, key = prepare_exam(exam)
    return profile, raw, exam, packet, key


def _gec_fixture() -> tuple[dict, bytes, dict, dict]:
    profile = {
        "id": "ua-gec-public-gec-only-test",
        "revision": "synthetic-gec-r1",
        "source_sha256": "0" * 64,
        "license": "synthetic-license",
        "source_path": "synthetic.m2",
        "denominator": {"sentences": 1, "documents": 0, "tokens": 3},
    }
    raw = (
        b"S \xd0\xa6\xd0\xb5 \xd1\x82\xd0\xb5\xd1\x81\xd1\x82 .\n"
        b"A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||0\n"
        b"A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||1\n\n"
    )
    profile["source_sha256"] = hashlib.sha256(raw).hexdigest()
    provenance = {
        "source_url": "https://example.test/gec",
        "source_revision": profile["revision"],
        "license": profile["license"],
        "exposure": "synthetic",
    }
    packet, key = prepare_gec(raw.decode("utf-8"), provenance)
    return profile, raw, packet, key


def test_verify_valid_nmt_manifest_is_hash_bound_and_honest() -> None:
    profile, raw, exam, packet, key, overlay = _nmt_fixture()

    manifest = verify_benchmark(profile, raw, packet, key, exam=exam, overlay=overlay)

    assert set(manifest) == {
        "schema",
        "suite_id",
        "verification",
        "profile_sha256",
        "source_sha256",
        "packet_sha256",
        "key_sha256",
        "exam_sha256",
        "overlay_sha256",
        "denominator",
        "license",
        "source_revision",
    }
    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["suite_id"] == profile["id"]
    assert manifest["verification"] == "matches_supplied_profile"
    assert manifest["profile_sha256"] == digest(profile)
    assert manifest["source_sha256"] == profile["source_sha256"]
    assert manifest["packet_sha256"] == packet["packet_sha256"]
    assert manifest["key_sha256"] == digest(key)
    assert manifest["exam_sha256"] == digest(exam)
    assert manifest["overlay_sha256"] == digest(overlay)
    assert manifest["denominator"] == profile["denominator"]
    assert manifest["license"] == profile["license"]
    assert manifest["source_revision"] == profile["revision"]
    assert "Choose" not in repr(manifest)
    assert "correct" not in repr(manifest)


def test_verify_valid_ulp_manifest_accepts_explicit_synthetic_denominator() -> None:
    profile, raw, exam, packet, key = _ulp_fixture()

    manifest = verify_benchmark(profile, raw, packet, key, exam=exam)

    assert manifest["suite_id"] == "ulp"
    assert manifest["verification"] == "matches_supplied_profile"
    assert manifest["denominator"] == {"items": 2, "points": 2}
    assert manifest["exam_sha256"] == digest(exam)
    assert manifest["overlay_sha256"] is None
    assert manifest["key_sha256"] == digest(key)


def test_verify_valid_gec_manifest_reconstructs_both_annotators() -> None:
    profile, raw, packet, key = _gec_fixture()

    manifest = verify_benchmark(profile, raw, packet, key)

    assert manifest["suite_id"] == "ua-gec-public-gec-only-test"
    assert manifest["verification"] == "matches_supplied_profile"
    assert manifest["packet_sha256"] == packet["packet_sha256"]
    assert manifest["key_sha256"] == digest(key)
    assert manifest["exam_sha256"] is None
    assert manifest["overlay_sha256"] is None
    assert manifest["denominator"] == profile["denominator"]


def test_profile_and_source_hashes_are_required() -> None:
    profile, raw, exam, packet, key = _ulp_fixture()
    wrong_profile_hash = copy.deepcopy(profile)
    wrong_profile_hash["source_sha256"] = "0" * 64
    with pytest.raises(ExamError, match="source hash"):
        verify_benchmark(wrong_profile_hash, raw, packet, key, exam=exam)

    wrong_format = copy.deepcopy(profile)
    wrong_format["source_sha256"] = "A" * 64
    with pytest.raises(ExamError, match="lowercase SHA-256"):
        verify_benchmark(wrong_format, raw, packet, key, exam=exam)


def test_rewritten_or_reordered_mcq_exam_fails_even_with_recomputed_artifacts() -> None:
    profile, raw, exam, _packet, _key = _ulp_fixture()
    rewritten = copy.deepcopy(exam)
    rewritten["items"][0]["question"] = "Rewritten question."
    rewritten_packet, rewritten_key = prepare_exam(rewritten)
    with pytest.raises(ExamError, match="reconstructed exam"):
        verify_benchmark(profile, raw, rewritten_packet, rewritten_key, exam=rewritten)

    reordered = copy.deepcopy(exam)
    reordered["items"].reverse()
    reordered_packet, reordered_key = prepare_exam(reordered)
    with pytest.raises(ExamError, match="reconstructed exam"):
        verify_benchmark(profile, raw, reordered_packet, reordered_key, exam=reordered)


def test_tampered_key_fails_after_caller_recomputes_internal_hash() -> None:
    profile, raw, exam, packet, key = _ulp_fixture()
    tampered = copy.deepcopy(key)
    tampered["answers"]["q0001"] = "Б"
    key_body = {field: tampered[field] for field in tampered if field != "key_sha256"}
    tampered["key_sha256"] = digest(key_body)

    with pytest.raises(ExamError, match="supplied key"):
        verify_benchmark(profile, raw, packet, tampered, exam=exam)


def test_profile_denominator_and_provenance_must_match_source_contract() -> None:
    profile, raw, exam, packet, key = _ulp_fixture()
    wrong_count = copy.deepcopy(profile)
    wrong_count["denominator"]["items"] = 3
    wrong_count["denominator"]["points"] = 3
    with pytest.raises(ExamError, match="denominator"):
        verify_benchmark(wrong_count, raw, packet, key, exam=exam)

    wrong_revision_exam = copy.deepcopy(exam)
    wrong_revision_exam["provenance"]["source_revision"] = "other-revision"
    wrong_packet, wrong_key = prepare_exam(wrong_revision_exam)
    with pytest.raises(ExamError, match="source_revision"):
        verify_benchmark(profile, raw, wrong_packet, wrong_key, exam=wrong_revision_exam)


def test_suite_and_overlay_boundaries_are_enforced() -> None:
    profile, raw, exam, packet, key = _ulp_fixture()
    unsupported = copy.deepcopy(profile)
    unsupported["id"] = "unapproved-suite"
    with pytest.raises(ExamError, match="unsupported benchmark suite"):
        verify_benchmark(unsupported, raw, packet, key, exam=exam)

    overlay = {"schema": TYPOGRAPHY_SCHEMA}
    with pytest.raises(ExamError, match="does not accept"):
        verify_benchmark(profile, raw, packet, key, exam=exam, overlay=overlay)

    nmt_profile, nmt_raw, nmt_exam, nmt_packet, nmt_key, nmt_overlay = _nmt_fixture()
    with pytest.raises(ExamError, match="requires a typography overlay"):
        verify_benchmark(nmt_profile, nmt_raw, nmt_packet, nmt_key, exam=nmt_exam)
    wrong_source_overlay = copy.deepcopy(nmt_overlay)
    wrong_source_overlay["official_source"]["sha256"] = "b" * 64
    with pytest.raises(ExamError, match="official source"):
        verify_benchmark(
            nmt_profile,
            nmt_raw,
            nmt_packet,
            nmt_key,
            exam=nmt_exam,
            overlay=wrong_source_overlay,
        )


def test_strict_jsonl_parser_rejects_duplicate_keys_non_utf8_and_nonfinite_values() -> None:
    profile, raw, exam, packet, key = _ulp_fixture()
    duplicate_line = (
        b'{"question":"Question one.","question":"changed","choices":["one","two","three"],'
        b'"answer":0,"answer_letter":"\xd0\x90","debug_id":"one","category":"grammar"}\n'
        + raw.splitlines()[1]
        + b"\n"
    )
    duplicate_profile = copy.deepcopy(profile)
    duplicate_profile["source_sha256"] = hashlib.sha256(duplicate_line).hexdigest()
    with pytest.raises(ExamError, match="duplicate JSON key"):
        verify_benchmark(duplicate_profile, duplicate_line, packet, key, exam=exam)

    nonfinite_line = raw.replace(b'"answer":0', b'"answer":NaN')
    nonfinite_profile = copy.deepcopy(profile)
    nonfinite_profile["source_sha256"] = hashlib.sha256(nonfinite_line).hexdigest()
    with pytest.raises(ExamError, match="non-finite JSON number"):
        verify_benchmark(nonfinite_profile, nonfinite_line, packet, key, exam=exam)

    invalid_bytes = b"\xff"
    invalid_profile = copy.deepcopy(profile)
    invalid_profile["source_sha256"] = hashlib.sha256(invalid_bytes).hexdigest()
    with pytest.raises(ExamError, match="valid UTF-8"):
        verify_benchmark(invalid_profile, invalid_bytes, packet, key, exam=exam)


def test_gec_rejects_exam_overlay_key_revision_and_denominator_tampering() -> None:
    profile, raw, packet, key = _gec_fixture()
    with pytest.raises(ExamError, match="does not accept an exam"):
        verify_benchmark(profile, raw, packet, key, exam={})
    with pytest.raises(ExamError, match="does not accept a typography overlay"):
        verify_benchmark(profile, raw, packet, key, overlay={})

    wrong_key = copy.deepcopy(key)
    wrong_key["provenance"]["source_revision"] = "other-revision"
    with pytest.raises(ExamError, match="source_revision"):
        verify_benchmark(profile, raw, packet, wrong_key)

    wrong_count = copy.deepcopy(profile)
    wrong_count["denominator"]["sentences"] = 2
    with pytest.raises(ExamError, match="denominator"):
        verify_benchmark(wrong_count, raw, packet, key)

    tampered_key = copy.deepcopy(key)
    tampered_key["items"][0]["annotations"][0]["category"] = "Spelling"
    tampered_key["key_sha256"] = digest({field: tampered_key[field] for field in tampered_key if field != "key_sha256"})
    with pytest.raises(ExamError, match="supplied key"):
        verify_benchmark(profile, raw, packet, tampered_key)


def test_profile_digest_distinguishes_synthetic_count_profile_from_release_profile() -> None:
    profile, raw, exam, packet, key = _ulp_fixture()
    release_shaped = copy.deepcopy(profile)
    release_shaped["denominator"] = {"items": 347, "points": 347}
    assert digest(profile) != digest(release_shaped)
    manifest = verify_benchmark(profile, raw, packet, key, exam=exam)
    assert manifest["profile_sha256"] == digest(profile)
    assert manifest["verification"] == "matches_supplied_profile"
