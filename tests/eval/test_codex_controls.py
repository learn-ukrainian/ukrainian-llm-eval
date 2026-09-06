"""Deterministic receipt construction tests for the local Codex control probe."""

from __future__ import annotations

import gzip
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ukrainian_llm_eval import adapters, codex_controls, native_codex


def _probe() -> native_codex._CliProbe:
    return native_codex._CliProbe(
        "codex-fixture",
        Path("/fixture/codex"),
        "a" * 64,
        Path("/fixture/native-codex"),
        "b" * 64,
        "codex-cli fixture",
    )


def _captures(tmp_path: Path, *, inert: bool = True) -> dict[str, dict[str, Any]]:
    captures = {}
    for name in native_codex._INERT_HANDLER_NAMES:
        artifact = tmp_path / f"{name.replace('.', '-')}.json"
        artifact.write_bytes(adapters.canonical({"handler": name, "inert": inert}).encode())
        artifact.chmod(0o600)
        captures[name] = {
            "capture": {"inert": inert},
            "path": artifact,
            "sha256": codex_controls._sha256(artifact.read_bytes()),
        }
    return captures


def test_receipt_binds_the_exact_requested_model_and_capture_hashes(tmp_path: Path) -> None:
    receipt = codex_controls._build_receipt(
        _probe(),
        model="gpt-5.6-luna",
        effort="medium",
        captures=_captures(tmp_path),
    )

    assert receipt["request_shape_sha256"] == adapters.digest(
        native_codex._request_shape({"model": "gpt-5.6-luna", "effort": "medium"})
    )
    assert receipt["request_shape_sha256"] != adapters.digest(
        native_codex._request_shape({"model": "gpt-5.6-sol", "effort": "medium"})
    )
    assert set(receipt["handler_evidence"]) == native_codex._INERT_HANDLER_NAMES
    assert all(item["source_kind"] == "local-mock-capture" for item in receipt["handler_evidence"].values())


def test_receipt_refuses_any_unproven_handler(tmp_path: Path) -> None:
    captures = _captures(tmp_path)
    captures["functions.wait"]["capture"]["inert"] = False
    with pytest.raises(codex_controls.ControlProbeError, match="did not prove"):
        codex_controls._build_receipt(_probe(), model="gpt-5.6-luna", effort="medium", captures=captures)


def test_expected_output_requires_the_injected_call_and_normalizes_default_mode() -> None:
    case = codex_controls._HandlerCase(*codex_controls._HANDLERS[2])
    assert not codex_controls._expected_output(
        case,
        [{"type": "function_call_output", "call_id": "another-call", "text": "request_user_input is unavailable in Default mode"}],
    )
    assert codex_controls._expected_output(
        case,
        [{"type": "function_call_output", "call_id": codex_controls._CALL_ID, "text": "request_user_input is unavailable in Default mode"}],
    )


def test_advertisement_rejects_unknown_or_missing_tools() -> None:
    case = codex_controls._HandlerCase(*codex_controls._HANDLERS[0])
    exact = {
        "tool_surface_valid": True,
        "top_level_tool_count": 0,
        "top_level_tool_names": [],
        "additional_tool_namespaces": {"functions": sorted(codex_controls._EXPECTED_FUNCTIONS)},
    }
    assert codex_controls._advertisement_matches(case, exact)
    assert not codex_controls._advertisement_matches(
        case,
        {
            **exact,
            "additional_tool_namespaces": {"functions": [*codex_controls._EXPECTED_FUNCTIONS, "other"]},
        },
    )
    assert not codex_controls._advertisement_matches(
        case,
        {**exact, "additional_tool_namespaces": {"functions": ["exec", "wait"]}},
    )


def test_surface_summary_rejects_unnamed_top_tools_and_duplicate_namespaces() -> None:
    unnamed = codex_controls._request_summary(b'{"tools":[{"type":"web_search"}],"input":[]}')
    duplicate = codex_controls._request_summary(
        b'{"tools":[],"input":[{"type":"additional_tools","tools":['
        b'{"name":"functions","tools":[{"name":"exec"}]},'
        b'{"name":"functions","tools":[{"name":"wait"}]}]}]}'
    )
    case = codex_controls._HandlerCase(*codex_controls._HANDLERS[0])
    assert unnamed["top_level_tool_count"] == 1
    assert unnamed["tool_surface_valid"] is False
    assert duplicate["tool_surface_valid"] is False
    assert not codex_controls._advertisement_matches(case, unnamed)
    assert not codex_controls._advertisement_matches(case, duplicate)


def test_compressed_and_process_capture_limits_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_controls, "_MAX_DECODED_BODY_BYTES", 8)
    assert codex_controls._decode_body(gzip.compress(b"x" * 9)) is None
    monkeypatch.setattr(codex_controls, "_MAX_PROCESS_BYTES", 16)
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 32)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr, timed_out, overflow = codex_controls._bounded_process_output(process)
    assert overflow and not timed_out
    assert len(stdout) <= 16 and stderr == b""


def test_synthetic_probe_schema_reuses_the_shared_packet_schema() -> None:
    schema = adapters.response_schema(codex_controls._SYNTHETIC_PACKET)
    responses = schema["properties"]["responses"]
    assert responses["required"] == ["synthetic-1"]
    assert schema["additionalProperties"] is False


def test_run_probe_writes_immutable_captures_and_receipt_without_live_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(codex_controls.native_codex, "_probe_cli", lambda *_args: _probe())

    def fake_case(root: Path, _probe_value: native_codex._CliProbe, _model: str, _effort: str, case: Any) -> dict[str, Any]:
        case_root = root / case.receipt_name.replace(".", "-")
        case_root.mkdir(mode=0o700)
        artifact = case_root / "capture.json"
        artifact.write_bytes(adapters.canonical({"handler": case.receipt_name, "inert": True}).encode())
        artifact.chmod(0o600)
        return {
            "capture": {"inert": True},
            "path": artifact,
            "sha256": codex_controls._sha256(artifact.read_bytes()),
        }

    monkeypatch.setattr(codex_controls, "_run_case", fake_case)
    output = tmp_path / "new-control-capture"
    result = codex_controls.run_probe(output, codex_bin="codex-fixture", model="gpt-5.6-luna", effort="medium")

    receipt_path = output / native_codex.CODEX_CONTROL_FILE
    assert result["report"]["status"] == "passed"
    assert receipt_path.exists()
    assert (output / "report.json").exists()
    with pytest.raises(codex_controls.ControlProbeError, match="new absolute directory"):
        codex_controls.run_probe(output, codex_bin="codex-fixture", model="gpt-5.6-luna", effort="medium")
