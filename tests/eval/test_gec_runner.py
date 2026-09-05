from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ukrainian_llm_eval import adapters, runner
from ukrainian_llm_eval.core import ExamError, digest, prepare_exam
from ukrainian_llm_eval.gec import GEC_PACKET_SCHEMA


def _gec_packet() -> dict[str, Any]:
    items = [
        {"id": "q0001", "text": "Це речення ."},
        {"id": "q0002", "text": "Без помилки ."},
        {"id": "q0003", "text": "Ще один приклад ."},
    ]
    body = {"schema": GEC_PACKET_SCHEMA, "items": items}
    return {**body, "packet_sha256": digest(body)}


def _config(**extra: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "schema": "zno-nmt.config.v1",
        "adapter": "chat-http",
        "model": "local-test-model",
        "effort": None,
        "timeout_seconds": 15,
        "max_output_tokens": 200,
        "max_tool_calls": 2,
        "repeats": 1,
        "tools": ["verify_word"],
        "corpus_id": "fixture-corpus",
        "endpoint_env": "GEC_ENDPOINT",
        "key_env": "GEC_KEY",
    }
    config.update(extra)
    return config


def _trial(responses: dict[str, Any]) -> dict[str, Any]:
    return {
        "responses": responses,
        "identity": {
            "adapter": "chat-http",
            "harness": "chat-http",
            "model": "local-test-model",
            "effective_effort": "unknown",
        },
        "metrics": {
            "elapsed_seconds": 0.1,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "tool_calls": 0,
        },
    }


def test_gec_runner_validates_source_packet_and_preserves_all_items(monkeypatch: pytest.MonkeyPatch) -> None:
    packet = _gec_packet()
    captured: dict[str, Any] = {}
    capability = {"tool_schema_sha256": "a" * 64, "mcp_server_identity_sha256": None}

    monkeypatch.setattr(runner, "preflight", lambda *_args, **_kwargs: capability)

    def fake_run(
        packet_arg: dict[str, Any],
        _config_arg: dict[str, Any],
        condition: str,
        *,
        sources_url: str | None,
        prompt: str,
        evidence: Any = None,
    ) -> dict[str, Any]:
        captured.update(packet=packet_arg, condition=condition, sources_url=sources_url, prompt=prompt)
        if evidence is not None:
            evidence("completion_response", {"model": "local-test-model", "raw": "caller supplied"})
        return _trial({"q0001": "Це речення.", "q0002": None, "q0003": "Ще один приклад ."})

    monkeypatch.setattr(runner.adapters, "run_chat_http", fake_run)
    events: list[tuple[str, Any]] = []
    result = runner.run_exam(
        packet,
        _config(),
        "closed-book",
        evidence=lambda kind, payload: events.append((kind, payload)),
    )

    assert result["schema"] == "ua-gec.run.v1"
    assert result["status"] == "ok"
    assert set(result["responses"]) == {"q0001", "q0002", "q0003"}
    assert result["responses"]["q0002"] is None
    assert len(captured["packet"]["items"]) == len(packet["items"]) == 3
    assert captured["condition"] == "closed-book"
    assert "Ukrainian grammatical-error correction" in captured["prompt"]
    assert "preserving the original meaning" in captured["prompt"]
    assert "original sentence unchanged" in captured["prompt"]
    assert "annotations" not in captured["prompt"]
    assert "answer key" not in captured["prompt"]
    assert [kind for kind, _ in events] == [
        "trial_input",
        "preflight",
        "prompt",
        "completion_response",
    ]


def test_gec_source_prompt_keeps_reference_policy_and_gold_free_task() -> None:
    prompt = adapters.build_prompt(_gec_packet(), "sources", max_tool_calls=4)

    assert "Ukrainian grammatical-error correction" in prompt
    assert "preserving the original meaning" in prompt
    assert "original sentence unchanged" in prompt
    assert "Only the explicitly provided Sources reference tools may be used." in prompt
    assert "at most 4 total reference-tool calls" in prompt
    assert "scoring key" not in prompt
    assert "annotations" not in prompt
    assert "replacement" not in prompt


def test_gec_http_transport_returns_strings_or_null_and_uses_gec_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _gec_packet()
    config = _config()
    monkeypatch.setenv("GEC_ENDPOINT", "https://example.invalid/chat")
    captured: dict[str, Any] = {}

    def completion(_url: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured["payload"] = payload
        return {
            "model": "local-test-model",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"responses": {"q0001": "Виправлене речення.", "q0002": None, "q0003": "Ще один приклад ."}}
                        )
                    }
                }
            ],
            "usage": {},
        }

    monkeypatch.setattr(adapters, "_http_json", completion)
    result = adapters.run_chat_http(packet, config, "closed-book", sources_url=None, prompt="exact GEC prompt")

    assert result["responses"] == {
        "q0001": "Виправлене речення.",
        "q0002": None,
        "q0003": "Ще один приклад .",
    }
    response_format = captured["payload"]["response_format"]["json_schema"]
    assert response_format["name"] == "ua_gec_responses"
    q0001 = response_format["schema"]["properties"]["responses"]["properties"]["q0001"]
    assert q0001["anyOf"][0]["type"] == "string"
    assert q0001["anyOf"][0]["pattern"] == "^[^\\r\\n\\u0085\\u2028\\u2029]+$"


@pytest.mark.parametrize(
    "malformed",
    [
        {"corrected": "object is not a sentence"},
        "",
        "two lines\nare invalid",
        "trailing newline\n",
        17,
        [],
    ],
)
def test_gec_malformed_http_values_preserve_raw_response_before_rejection(
    monkeypatch: pytest.MonkeyPatch, malformed: Any
) -> None:
    packet = _gec_packet()
    config = _config()
    monkeypatch.setenv("GEC_ENDPOINT", "https://example.invalid/chat")
    body = {
        "model": "local-test-model",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"responses": {"q0001": malformed, "q0002": None, "q0003": "Валідне речення."}}
                    )
                }
            }
        ],
    }
    events: list[tuple[str, Any]] = []
    monkeypatch.setattr(adapters, "_http_json", lambda *_args, **_kwargs: body)

    with pytest.raises(adapters.AdapterError, match="GEC response value"):
        adapters.run_chat_http(
            packet,
            config,
            "closed-book",
            sources_url=None,
            prompt="exact GEC prompt",
            evidence=lambda kind, payload: events.append((kind, payload)),
        )

    response_events = [payload for kind, payload in events if kind == "completion_response"]
    assert response_events == [body]


def test_gec_runner_failure_is_typed_and_retains_raw_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    packet = _gec_packet()
    capability = {"tool_schema_sha256": "b" * 64, "mcp_server_identity_sha256": None}
    monkeypatch.setattr(runner, "preflight", lambda *_args, **_kwargs: capability)
    calls = 0

    def fail(
        *_args: Any,
        evidence: Any = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if evidence is not None:
            evidence("completion_response", {"raw": "malformed completion"})
        raise adapters.AdapterError("provider GEC response value is invalid")

    monkeypatch.setattr(runner.adapters, "run_chat_http", fail)
    events: list[tuple[str, Any]] = []
    result = runner.run_exam(
        packet,
        _config(),
        "closed-book",
        evidence=lambda kind, payload: events.append((kind, payload)),
    )

    assert calls == 1
    assert result["schema"] == "ua-gec.run.v1"
    assert result["status"] == "failed"
    assert result["responses"] == {"q0001": None, "q0002": None, "q0003": None}
    assert [kind for kind, _ in events][-2:] == ["completion_response", "trial_failure"]
    assert events[-2][1] == {"raw": "malformed completion"}


def test_gec_run_comparison_binds_response_and_validator_implementations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _gec_packet()
    comparison = runner._comparison(packet, _config())

    assert comparison["schema"] == "ua-gec.comparison.v1"
    assert comparison["constants_sha256"]
    original_gec = Path(runner.gec.__file__)
    changed_gec = tmp_path / "gec.py"
    changed_gec.write_bytes(original_gec.read_bytes() + b"\n# implementation change\n")
    monkeypatch.setattr(runner.gec, "__file__", str(changed_gec))
    changed = runner._comparison(packet, _config())
    assert changed["constants_sha256"] != comparison["constants_sha256"]


def test_gec_packet_cannot_carry_a_grading_key_and_mcq_schema_stays_unchanged() -> None:
    packet_with_key = _gec_packet()
    packet_with_key["key"] = {"answers": "must stay private"}
    with pytest.raises(ExamError):
        runner._validated_packet(packet_with_key)

    exam = {
        "schema": "zno-nmt.exam.v1",
        "title": "fixture",
        "subject": "Ukrainian",
        "year": 2022,
        "provenance": {
            "source_url": "https://example.invalid",
            "source_revision": "fixture",
            "license": "test",
            "exposure": "synthetic",
        },
        "scoring": {
            "kind": "benchmark",
            "policy_url": None,
            "pass_threshold": None,
            "expected_items": 1,
            "expected_points": 1,
        },
        "items": [
            {
                "id": "1",
                "kind": "single",
                "question": "Питання",
                "options": [{"id": "A", "text": "Варіант"}],
                "rows": [],
                "correct": "A",
            }
        ],
    }
    mcq_packet, _private_key = prepare_exam(exam)
    assert runner._run_schema(mcq_packet) == "zno-nmt.run.v1"
    assert adapters.response_schema(mcq_packet)["properties"]["responses"]["properties"]["q0001"]["anyOf"][0] == {
        "type": "string"
    }


def test_gec_pair_resume_retains_interrupted_trial_and_never_repeats_completed_calls(tmp_path, monkeypatch):
    from ukrainian_llm_eval import scheduling
    from ukrainian_llm_eval.evidence import EvidenceStore

    capability = {"tool_schema_sha256": "a" * 64, "mcp_server_identity_sha256": None}
    monkeypatch.setattr(runner, "preflight", lambda *args, **kwargs: capability)
    monkeypatch.setattr(scheduling, "preflight", lambda *args, **kwargs: capability)
    calls = []

    def transport(packet, config, condition, *, prompt, evidence, **kwargs):
        calls.append(condition)
        evidence("synthetic_transport", {"condition": condition})
        if len(calls) == 1:
            raise KeyboardInterrupt
        return _trial({item["id"]: item["text"] for item in packet["items"]})

    monkeypatch.setattr(adapters, "run_chat_http", transport)
    root = tmp_path / "pair"
    with pytest.raises(KeyboardInterrupt):
        list(scheduling.run_pair(_gec_packet(), _config(), root))
    progress = list(scheduling.run_pair(_gec_packet(), _config(), root, resume=True))
    assert calls == ["closed-book", "sources"]
    assert progress[0]["status"] == "failed" and progress[-1]["failed"] is True
    receipts = EvidenceStore(root / "evidence").verify_all()
    assert receipts["r001-closed-book"]["terminal_status"] == "interrupted"
    assert receipts["r001-closed-book"]["result"]["schema"] == "ua-gec.run.v1"
    assert receipts["r001-sources"]["result"]["schema"] == "ua-gec.run.v1"
    before = {path.name: path.read_bytes() for path in root.glob("*.json")}
    list(scheduling.run_pair(_gec_packet(), _config(), root, resume=True))
    assert calls == ["closed-book", "sources"]
    assert before == {path.name: path.read_bytes() for path in root.glob("*.json")}
