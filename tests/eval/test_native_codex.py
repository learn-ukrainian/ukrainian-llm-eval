"""Deterministic controls for the native Codex adapter.

These tests use only a local subprocess-shaped mock.  They do not call Codex,
read a real auth home, or make network requests.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ukrainian_llm_eval import native_codex
from ukrainian_llm_eval.candidate_outcome import CANDIDATE_RESPONSE_ERROR, is_candidate_response_failure


def _packet() -> dict[str, Any]:
    return {
        "schema": "zno-nmt.questions.v1", "packet_sha256": "a" * 64,
        "items": [{"id": "opaque-1", "kind": "single", "question": "Питання", "options": [{"id": "A", "text": "Варіант"}], "rows": []}],
    }


def _matching_packet() -> dict[str, Any]:
    return {
        "schema": "zno-nmt.questions.v1", "packet_sha256": "m" * 64,
        "items": [
            {"id": "single-1", "kind": "single", "question": "Питання", "options": [], "rows": []},
            {"id": "matching-1", "kind": "matching", "question": "Зіставте", "options": [], "rows": [{"id": "row-a"}, {"id": "row-b"}]},
        ],
    }


def _gec_packet() -> dict[str, Any]:
    return {
        "schema": native_codex.adapters.GEC_PACKET_SCHEMA, "packet_sha256": "g" * 64,
        "items": [{"id": "gec-1", "sentence": "Це тест."}],
    }


def _config(binary: str = "codex-fixture", **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "zno-nmt.config.v1", "adapter": "codex", "model": "gpt-5.6-luna", "effort": "ultra",
        "timeout_seconds": 15, "max_output_tokens": 100, "max_tool_calls": 1, "repeats": 1,
        "tools": [], "corpus_id": None, "provider": native_codex.CODEX_PROVIDER, "codex_bin": binary,
    }
    value.update(extra)
    return value


def _probe(binary: str = "codex-fixture") -> native_codex._CliProbe:
    return native_codex._CliProbe(
        binary, Path(binary), "b" * 64, Path("codex-native-fixture"), "c" * 64,
        "codex-cli 0.153.2",
    )


def _provisioning(
    tmp_path: Path,
    config: dict[str, Any],
    *,
    controls: dict[str, str] | None = None,
    auth: dict[str, Any] | None = None,
) -> Path:
    root = tmp_path / "private-codex"
    root.mkdir(mode=0o700)
    auth = auth or {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": "2026-09-05T00:00:00Z",
        "tokens": {"access_token": "fixture-access", "account_id": "fixture-account", "id_token": "fixture-id", "refresh_token": "fixture-refresh"},
    }
    (root / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    (root / "auth.json").chmod(0o600)
    artifact = tmp_path / "handler-mock-capture.json"
    artifact.write_text('{"fixture":"inert-handlers"}', encoding="utf-8")
    artifact_hash = native_codex.hashlib.sha256(artifact.read_bytes()).hexdigest()
    receipt = {
        "schema": "native-codex-control.v2", "entrypoint_sha256": "b" * 64,
        "native_runtime_sha256": "c" * 64, "cli_version": "codex-cli 0.153.2",
        "probe_implementation_sha256": native_codex._probe_implementation_sha256(),
        "request_shape_sha256": native_codex.adapters.digest(native_codex._request_shape(config)),
        "fresh_neutral_cwd": True, "fresh_home": True, "ignore_user_config": True, "ignore_rules": True, "ephemeral": True,
        "handler_controls": controls or {name: "inert" for name in native_codex._INERT_HANDLER_NAMES},
        "handler_evidence": {
            name: {"source_kind": "local-mock-capture", "artifact_path": str(artifact), "artifact_sha256": artifact_hash, "request_shape_sha256": native_codex.adapters.digest(native_codex._request_shape(config))}
            for name in native_codex._INERT_HANDLER_NAMES
        },
    }
    (root / "closed-book-control.json").write_text(json.dumps(receipt), encoding="utf-8")
    (root / "closed-book-control.json").chmod(0o600)
    return root


def test_validator_rejects_ambiguous_model_tools_and_unknown_fields() -> None:
    assert native_codex.validate_codex_config({"model": "gpt-5.6-luna"})["codex_bin"] == "codex"
    with pytest.raises(native_codex.CodexAdapterError, match="exact gpt"):
        native_codex.validate_codex_config({"model": "gpt-5.6-luna latest"})
    with pytest.raises(native_codex.CodexAdapterError, match="closed-book only"):
        native_codex.validate_config(_config(tools=["verify_word"]))
    with pytest.raises(native_codex.CodexAdapterError, match="unsupported fields"):
        native_codex.validate_config(_config(unexpected=True))


@pytest.mark.parametrize(("include_provider", "provider"), [(True, None), (False, None)])
def test_validator_normalizes_default_subscription_provider(include_provider: bool, provider: str | None) -> None:
    config = _config(provider=provider)
    if not include_provider:
        config.pop("provider", None)
    assert native_codex.validate_config(config)["provider"] == native_codex.CODEX_PROVIDER


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (12, 12),
        (True, None),
        (-1, None),
        (1.5, None),
        (float("inf"), None),
        (float("nan"), None),
    ],
)
def test_usage_rejects_non_integer_or_nonfinite_counters(value: Any, expected: int | None) -> None:
    assert native_codex._usage(value) == expected


def _assert_closed_object_nodes(schema: dict[str, Any]) -> None:
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert isinstance(schema["required"], list)
        for value in schema.get("properties", {}).values():
            _assert_closed_object_nodes(value)
    for branch in schema.get("anyOf", []):
        _assert_closed_object_nodes(branch)


def test_packet_response_schema_has_exact_question_and_row_keys() -> None:
    schema = native_codex.adapters.response_schema(_matching_packet())
    _assert_closed_object_nodes(schema)
    responses = schema["properties"]["responses"]
    assert responses["required"] == ["single-1", "matching-1"]
    assert set(responses["properties"]) == {"single-1", "matching-1"}
    matching = responses["properties"]["matching-1"]["anyOf"][0]
    assert matching["required"] == ["row-a", "row-b"]
    assert set(matching["properties"]) == {"row-a", "row-b"}


def test_gec_response_schema_has_exact_question_keys_and_closed_objects() -> None:
    schema = native_codex.adapters.response_schema(_gec_packet())
    _assert_closed_object_nodes(schema)
    responses = schema["properties"]["responses"]
    assert responses["required"] == ["gec-1"]
    assert set(responses["properties"]) == {"gec-1"}


def test_public_preflight_rejects_sources_without_a_provider_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    config = _config()
    private = _provisioning(tmp_path, config)
    monkeypatch.setenv("UKRAINIAN_LLM_EVAL_CODEX_PROVISIONING_DIR", str(private))
    with pytest.raises(native_codex.CodexAdapterError, match="Sources is unsupported"):
        native_codex.adapters.preflight(config, "sources", "https://sources.invalid")


def test_preflight_requires_full_handler_effect_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config()
    private = _provisioning(tmp_path, config, controls={"functions.exec": "inert"})
    monkeypatch.setattr(native_codex, "_probe_cli", lambda *_args: _probe())
    with pytest.raises(native_codex.CodexAdapterError, match="handler controls are incomplete"):
        native_codex.preflight_codex(config, "closed-book", private_env_path=private)


def test_preflight_binds_control_receipt_to_request_shape_and_native_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config()
    private = _provisioning(tmp_path, config)
    monkeypatch.setattr(native_codex, "_probe_cli", lambda *_args: _probe())
    capability = native_codex.preflight_codex(config, "closed-book", private_env_path=private)
    assert capability["requested_effort"] == "ultra"
    assert capability["effective_effort"] == "unknown"
    assert capability["capability"] == "native-codex-isolated-controls"
    assert capability["entrypoint_sha256"] == "b" * 64
    assert capability["native_runtime_sha256"] == "c" * 64
    receipt = json.loads((private / "closed-book-control.json").read_text(encoding="utf-8"))
    receipt["native_runtime_sha256"] = "a" * 64
    (private / "closed-book-control.json").write_text(json.dumps(receipt), encoding="utf-8")
    (private / "closed-book-control.json").chmod(0o600)
    with pytest.raises(native_codex.CodexAdapterError, match="identity drift"):
        native_codex.preflight_codex(config, "closed-book", private_env_path=private)


def test_preflight_rejects_control_receipt_captured_for_another_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    config = _config()
    private = _provisioning(tmp_path, config)
    monkeypatch.setattr(native_codex, "_probe_cli", lambda *_args: _probe())
    changed_model = _config(model="gpt-5.6-sol")
    with pytest.raises(native_codex.CodexAdapterError, match="request shape drift"):
        native_codex.preflight_codex(changed_model, "closed-book", private_env_path=private)


def test_preflight_rejects_stale_control_probe_implementation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    config = _config()
    private = _provisioning(tmp_path, config)
    monkeypatch.setattr(native_codex, "_probe_cli", lambda *_args: _probe())
    receipt_path = private / native_codex.CODEX_CONTROL_FILE
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["probe_implementation_sha256"] = "d" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_path.chmod(0o600)
    with pytest.raises(native_codex.CodexAdapterError, match="probe implementation drift"):
        native_codex.preflight_codex(config, "closed-book", private_env_path=private)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_process_bounds_streams_and_preserves_partial_overflow_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stream: str,
) -> None:
    monkeypatch.setattr(native_codex, "_MAX_OUTPUT_BYTES", 16)
    evidence: list[tuple[str, Any]] = []
    with pytest.raises(native_codex.CodexAdapterError, match="output exceeds limit"):
        native_codex._run_process(
            [sys.executable, "-c", f"import sys; sys.{stream}.write('x' * 32); sys.{stream}.flush()"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            prompt="",
            timeout=5,
            evidence=lambda kind, payload: evidence.append((kind, payload)),
        )
    overflow = dict(evidence)["cli_output_overflow"]
    assert overflow[f"{stream}_truncated"] is True
    assert len(overflow[stream].encode("utf-8")) <= 16


def test_process_deadline_covers_blocked_large_stdin_write(tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(native_codex.CodexAdapterError, match="CLI timeout"):
        native_codex._run_process(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            prompt="x" * 1_000_000,
            timeout=1,
            evidence=None,
        )
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    ("argv", "timeout"),
    [
        ([sys.executable, "-c", "import sys; sys.stdout.write('x' * 3_000_000); sys.stdout.flush()"], 5),
        ([sys.executable, "-c", "import time; time.sleep(10)"], 1),
    ],
)
def test_process_evidence_callback_retains_its_open_file_descriptor(
    tmp_path: Path, argv: list[str], timeout: int,
) -> None:
    retained = tmp_path / "evidence-callback.txt"
    handles: list[Any] = []

    def evidence(_kind: str, _payload: Any) -> None:
        handle = retained.open("wb")
        handle.write(b"callback")
        handles.append(handle)

    with pytest.raises(native_codex.CodexAdapterError):
        native_codex._run_process(
            argv,
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            prompt="",
            timeout=timeout,
            evidence=evidence,
        )
    assert len(handles) == 1
    handles[0].write(b"-still-open")
    handles[0].close()
    assert retained.read_bytes() == b"callback-still-open"


def test_process_deadline_kills_descendant_that_keeps_parent_pipes_open(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "descendant.pid"
    started = time.monotonic()
    with pytest.raises(native_codex.CodexAdapterError, match="CLI timeout"):
        native_codex._run_process(
            [
                sys.executable,
                "-c",
                (
                    "import pathlib, subprocess, sys; "
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                    "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')"
                ),
                str(child_pid_path),
            ],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            prompt="",
            timeout=1,
            evidence=None,
        )
    assert time.monotonic() - started < 5
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("descendant remained alive after process-group cleanup")


def test_process_deadline_returns_when_escaped_descendant_keeps_pipes_open(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "escaped-descendant.pid"
    child_pid: int | None = None
    started = time.monotonic()
    try:
        with pytest.raises(native_codex.CodexAdapterError, match="CLI timeout"):
            native_codex._run_process(
                [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib, subprocess, sys; "
                        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
                        "start_new_session=True); "
                        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')"
                    ),
                    str(child_pid_path),
                ],
                cwd=tmp_path,
                env={"PATH": "/usr/bin:/bin"},
                prompt="",
                timeout=1,
                evidence=None,
            )
        assert time.monotonic() - started < 5
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        os.kill(child_pid, 0)
    finally:
        if child_pid_path.exists():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for _ in range(20):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("escaped descendant remained alive after test cleanup")


def test_process_rejects_invalid_utf8_with_bounded_raw_evidence(tmp_path: Path) -> None:
    evidence: list[tuple[str, Any]] = []
    with pytest.raises(native_codex.CodexAdapterError, match="invalid UTF-8"):
        native_codex._run_process(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff'); sys.stdout.flush()"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            prompt="",
            timeout=5,
            evidence=lambda kind, payload: evidence.append((kind, payload)),
        )
    invalid = dict(evidence)["cli_invalid_utf8"]
    assert invalid["invalid_utf8_streams"] == ["stdout"]
    assert invalid["stdout_raw_base64"] == "/w=="
    assert "stdout" not in invalid


def test_process_reports_multibyte_boundary_truncation_as_output_overflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(native_codex, "_MAX_OUTPUT_BYTES", 4)
    evidence: list[tuple[str, Any]] = []
    with pytest.raises(native_codex.CodexAdapterError, match="output exceeds limit"):
        native_codex._run_process(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abc\\xc3\\xa9'); sys.stdout.flush()"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            prompt="",
            timeout=5,
            evidence=lambda kind, payload: evidence.append((kind, payload)),
        )
    events = dict(evidence)
    overflow = events["cli_output_overflow"]
    assert "cli_invalid_utf8" not in events
    assert overflow["stdout_truncated"] is True
    assert overflow["invalid_utf8_streams"] == ["stdout"]
    assert base64.b64decode(overflow["stdout_raw_base64"]) == b"abc\xc3"


def test_process_preserves_valid_unicode_stdout(tmp_path: Path) -> None:
    completed = native_codex._run_process(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write('Привіт'.encode('utf-8'))"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        prompt="",
        timeout=5,
        evidence=None,
    )
    assert completed.stdout == "Привіт"


def _native_fixture(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture-native-runtime")
    path.chmod(0o700)
    return path


def test_native_runtime_resolution_accepts_direct_binary_and_known_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    direct = _native_fixture(tmp_path / "direct-codex")
    assert native_codex._native_runtime_for(direct) == direct

    monkeypatch.setattr(native_codex.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(native_codex.platform, "machine", lambda: "arm64")
    launcher = tmp_path / "@openai" / "codex" / "bin" / "codex.js"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env node\n// fixture launcher\n", encoding="utf-8")
    launcher.chmod(0o700)
    runtime = _native_fixture(
        tmp_path / "@openai" / "codex" / "node_modules" / "@openai" / "codex-darwin-arm64"
        / "vendor" / "aarch64-apple-darwin" / "bin" / "codex"
    )
    assert native_codex._native_runtime_for(launcher) == runtime


def test_native_runtime_resolution_rejects_unknown_wrapper(tmp_path: Path) -> None:
    wrapper = tmp_path / "codex-wrapper"
    wrapper.write_text("#!/bin/sh\nexec codex\n", encoding="utf-8")
    wrapper.chmod(0o700)
    with pytest.raises(native_codex.CodexAdapterError, match="runtime closure is unsupported"):
        native_codex._native_runtime_for(wrapper)


def test_probe_cli_records_direct_native_runtime_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    direct = _native_fixture(tmp_path / "direct-codex")

    def fake_checked(argv: list[str], _timeout: int) -> subprocess.CompletedProcess[str]:
        stdout = "codex-cli fixture\n" if argv[-1] == "--version" else "--ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --strict-config --json --output-schema --model --disable --enable"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(native_codex, "_run_checked", fake_checked)
    probe = native_codex._probe_cli(str(direct), 15)
    expected = native_codex.hashlib.sha256(direct.read_bytes()).hexdigest()
    assert probe.binary_path == direct
    assert probe.native_runtime_path == direct
    assert probe.entrypoint_sha256 == expected
    assert probe.native_runtime_sha256 == expected


def test_run_uses_fresh_home_structured_envelope_and_raw_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config()
    private = _provisioning(tmp_path, config)
    seen: dict[str, Any] = {}
    events: list[tuple[str, Any]] = []
    monkeypatch.setattr(native_codex, "_probe_cli", lambda *_args: _probe())

    def fake_process(argv: list[str], *, cwd: Path, env: dict[str, str], prompt: str, timeout: int, evidence: Any) -> subprocess.CompletedProcess[str]:
        seen.update(argv=argv, cwd=cwd, env=env, prompt=prompt, timeout=timeout)
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        seen["response_schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        copied_auth = json.loads(Path(env["CODEX_HOME"]).joinpath("auth.json").read_text(encoding="utf-8"))
        assert copied_auth["auth_mode"] == "chatgpt" and copied_auth["OPENAI_API_KEY"] is None
        stream = [
            {"type": "thread.started", "thread_id": "fresh-session"},
            {"type": "item.completed", "item": {"id": "warning", "type": "error", "message": "synthetic bootstrap warning"}},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"responses":{"opaque-1":"A"}}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}},
        ]
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(event) for event in stream), "private-stderr")

    monkeypatch.setattr(native_codex, "_run_process", fake_process)
    result = native_codex.run_codex(_packet(), config, "closed-book", sources_url=None, prompt="Return JSON", evidence=lambda kind, payload: events.append((kind, payload)), private_env_path=private)
    capability = native_codex.preflight_codex(config, "closed-book", private_env_path=private)
    assert result["identity"]["control_receipt_sha256"] == capability["control_receipt_sha256"]
    assert dict(events)["cli_invocation"]["control_receipt_sha256"] == capability["control_receipt_sha256"]
    assert result["responses"] == {"opaque-1": "A"}
    assert result["identity"]["session_id"] == "fresh-session"
    assert result["identity"]["requested_effort"] == "ultra"
    assert result["identity"]["accepted_effort"] == "unknown"
    assert result["identity"]["effective_effort"] == "unknown"
    assert result["identity"]["entrypoint_sha256"] == "b" * 64
    assert result["identity"]["native_runtime_sha256"] == "c" * 64
    assert dict(events)["cli_invocation"]["native_runtime_sha256"] == "c" * 64
    assert seen["response_schema"] == native_codex.adapters.response_schema(_packet())
    assert result["identity"]["response_schema_sha256"] == native_codex.adapters.digest(seen["response_schema"])
    assert dict(events)["cli_invocation"]["response_schema_sha256"] == result["identity"]["response_schema_sha256"]
    assert result["metrics"]["total_tokens"] is None
    assert seen["env"]["HOME"] != seen["env"]["CODEX_HOME"]
    assert "OPENAI_API_KEY" not in seen["env"]
    assert "--ignore-user-config" in seen["argv"] and "--ignore-rules" in seen["argv"]
    assert seen["argv"].count("code_mode") == 1 and "--enable" in seen["argv"]
    assert dict(events)["cli_result"]["stderr"] == "private-stderr"


@pytest.mark.parametrize("stream, message", [
    ([{"type": "thread.started", "thread_id": "fresh"}, {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}}], "preceded turn start"),
    ([{"type": "thread.started", "thread_id": "fresh"}, {"type": "turn.started"}, {"type": "item.completed", "item": {"type": "function_call"}}], "tool event"),
])
def test_parser_rejects_bad_lifecycle_and_tool_envelope(stream: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(native_codex.CodexAdapterError, match=message):
        native_codex._parse_events("\n".join(json.dumps(event) for event in stream), _packet())


def test_parser_rejects_item_error_after_turn_started() -> None:
    stream = [
        {"type": "thread.started", "thread_id": "fresh"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "error", "message": "mid-turn failure"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"responses":{"opaque-1":"A"}}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    with pytest.raises(native_codex.CodexAdapterError, match="item error during a turn"):
        native_codex._parse_events("\n".join(json.dumps(event) for event in stream), _packet())


@pytest.mark.parametrize("answer_content", ["not-json", "{}"])
def test_invalid_answer_after_verified_events_preserves_identity_metrics_and_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer_content: str,
) -> None:
    config = _config()
    private = _provisioning(tmp_path, config)
    events: list[tuple[str, Any]] = []
    monkeypatch.setattr(native_codex, "_probe_cli", lambda *_args: _probe())

    def fake_process(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        stream = [
            {"type": "thread.started", "thread_id": "verified-session"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": answer_content}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}},
        ]
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(event) for event in stream), "")

    monkeypatch.setattr(native_codex, "_run_process", fake_process)
    result = native_codex.run_codex(
        _packet(), config, "closed-book", sources_url=None, prompt="Return JSON",
        evidence=lambda kind, payload: events.append((kind, payload)), private_env_path=private,
    )

    assert result["status"] == "failed"
    assert result["failure_reason"] == CANDIDATE_RESPONSE_ERROR
    assert result["responses"] == {"opaque-1": None}
    assert is_candidate_response_failure(result, expected_response_ids=["opaque-1"])
    assert result["identity"]["session_id"] == "verified-session"
    assert result["metrics"]["input_tokens"] == 3
    assert result["metrics"]["output_tokens"] == 4
    answer_event = dict(events)["candidate_answer_outcome"]
    assert answer_event["failure_reason"] == CANDIDATE_RESPONSE_ERROR
    assert answer_event["session_id"] == "verified-session"
    assert answer_event["tool_calls"] == 0
    invalid_metrics = {
        "nonzero_tools": {"tool_calls": 1},
        "missing_input": {"input_tokens": None},
        "missing_output": {"output_tokens": None},
        "boolean_input": {"input_tokens": True},
        "fractional_output": {"output_tokens": 4.5},
    }
    for replacement in invalid_metrics.values():
        invalid = {**result, "metrics": {**result["metrics"], **replacement}}
        assert not is_candidate_response_failure(invalid, expected_response_ids=["opaque-1"])


def test_private_provisioning_rejects_ambient_codex_home_content(tmp_path: Path) -> None:
    config = _config()
    private = _provisioning(tmp_path, config)
    (private / "config.toml").write_text("ambient = true", encoding="utf-8")
    (private / "config.toml").chmod(0o600)
    with pytest.raises(native_codex.CodexAdapterError, match="sanitized Codex provisioning"):
        native_codex.validate_options(config, private_env_path=private)


@pytest.mark.parametrize(
    ("auth", "message"),
    [
        ({"OPENAI_API_KEY": "api-key", "auth_mode": "chatgpt", "last_refresh": "now", "tokens": {"access_token": "access", "account_id": "account", "id_token": "id", "refresh_token": "refresh"}}, "API-key auth"),
        ({"OPENAI_API_KEY": None, "auth_mode": "apikey", "last_refresh": "now", "tokens": {"access_token": "access", "account_id": "account", "id_token": "id", "refresh_token": "refresh"}}, "not ChatGPT"),
        ({"OPENAI_API_KEY": None, "auth_mode": "chatgpt", "last_refresh": "now", "tokens": {"access_token": "access", "account_id": "account", "id_token": "id"}}, "tokens are incomplete"),
    ],
)
def test_private_auth_rejects_non_subscription_or_incomplete_tokens(tmp_path: Path, auth: dict[str, Any], message: str) -> None:
    config = _config()
    private = _provisioning(tmp_path, config, auth=auth)
    with pytest.raises(native_codex.CodexAdapterError, match=message):
        native_codex.validate_options(config, private_env_path=private)


def test_private_auth_rejects_malformed_json_and_permissions(tmp_path: Path) -> None:
    config = _config()
    private = _provisioning(tmp_path, config)
    auth_path = private / "auth.json"
    auth_path.write_text("{", encoding="utf-8")
    auth_path.chmod(0o600)
    with pytest.raises(native_codex.CodexAdapterError, match="auth is invalid"):
        native_codex.validate_options(config, private_env_path=private)
    auth_path.write_text(json.dumps({"OPENAI_API_KEY": None, "auth_mode": "chatgpt", "last_refresh": "now", "tokens": {"access_token": "access", "account_id": "account", "id_token": "id", "refresh_token": "refresh"}}), encoding="utf-8")
    auth_path.chmod(0o644)
    with pytest.raises(native_codex.CodexAdapterError, match="owner-only"):
        native_codex.validate_options(config, private_env_path=private)
