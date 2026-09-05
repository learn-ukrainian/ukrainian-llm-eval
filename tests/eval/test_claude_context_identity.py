"""Native context modifiers require matching terminal backend attestation."""

import json
import subprocess

import pytest

from ukrainian_llm_eval import adapters


def _events():
    return [
        {"type": "system", "subtype": "init", "model": "claude-fixture[1m]", "tools": [], "session_id": "fresh"},
        {"type": "assistant", "message": {"model": "claude-fixture", "content": []}, "session_id": "fresh"},
        {"type": "result", "is_error": False, "session_id": "fresh", "result": '{"responses":{"q":"A"}}',
         "modelUsage": {"claude-fixture[1m]": {"canonicalModel": "claude-fixture", "contextWindow": 1000000}}},
    ]


def _stream(events):
    return "\n".join(map(json.dumps, events))


def _packet():
    return {"items": [{"id": "q", "kind": "single"}]}


def test_native_context_selector_records_requested_and_canonical_models(monkeypatch):
    config = {"schema": "zno-nmt.config.v1", "adapter": "claude", "model": "claude-fixture[1m]",
              "effort": "low", "timeout_seconds": 30, "max_output_tokens": 100,
              "max_tool_calls": 1, "repeats": 1, "tools": [], "corpus_id": None}
    monkeypatch.setattr(adapters, "_claude_capabilities", lambda *a, **k: ("claude", "fixture"))
    monkeypatch.setattr(adapters, "_run_claude_process", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, _stream(_events()), ""))
    monkeypatch.setattr(adapters, "response_schema", lambda p: {})
    trial = adapters.run_claude(_packet(), config, "closed-book", sources_url=None, prompt="Synthetic")
    assert trial["identity"]["requested_model"] == "claude-fixture[1m]"
    assert trial["identity"]["effective_model"] == "claude-fixture"
    assert trial["identity"]["model_context_mapping"] == {"claude-fixture[1m]": "claude-fixture"}
    assert trial["responses"] == {"q": "A"}


@pytest.mark.parametrize("attested", [True, False])
def test_research_controller_requires_native_attestation_for_context_selector(attested):
    from ukrainian_llm_eval.scheduling import _research_stop_reason

    config = {"adapter": "claude", "model": "claude-fixture[1m]", "effort": "low", "max_tool_calls": 1}
    identity = {"adapter": "claude", "effective_model": "claude-fixture", "effective_effort": "unknown"}
    if attested:
        identity["model_context_mapping"] = {"claude-fixture[1m]": "claude-fixture"}
    result = {"status": "ok", "identity": identity, "metrics": {}}
    route = {"billing": {"max_total_input_tokens": 100, "max_total_output_tokens": 100}}
    assert _research_stop_reason(result, route, config) == (None if attested else "model_identity_drift")


@pytest.mark.parametrize("mutation", ["missing", "wrong_canonical", "wrong_context", "extra_model", "assistant_drift"])
def test_context_modifier_never_masks_missing_attestation_or_model_drift(mutation):
    events = _events()
    record = events[-1]["modelUsage"]["claude-fixture[1m]"]
    if mutation == "missing":
        del events[-1]["modelUsage"]
    elif mutation == "wrong_canonical":
        record["canonicalModel"] = "claude-other"
    elif mutation == "wrong_context":
        record["contextWindow"] = 200000
    elif mutation == "extra_model":
        events[-1]["modelUsage"]["claude-other"] = dict(record)
    else:
        events[1]["message"]["model"] = "claude-other"
    with pytest.raises(adapters.AdapterError, match="model drift"):
        adapters._parse_stream_json(_stream(events), _packet(), set(), 1)
