"""Deterministic controls for the native Cursor adapter.

The fixture CLI is a local subprocess. No provider credentials or network
calls are used by these tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from ukrainian_llm_eval import native_cursor
from ukrainian_llm_eval.candidate_outcome import CANDIDATE_RESPONSE_ERROR, is_candidate_response_failure


def _packet() -> dict[str, Any]:
    return {
        "schema": "zno-nmt.questions.v1",
        "packet_sha256": "a" * 64,
        "items": [
            {
                "id": "opaque-1",
                "kind": "single",
                "question": "Питання",
                "options": [{"id": "A", "text": "Варіант"}],
                "rows": [],
            }
        ],
    }


def _config(binary: str, **extra: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "schema": "zno-nmt.config.v1",
        "adapter": "cursor",
        "model": "cursor-grok-4.6-high",
        "effort": "high",
        "timeout_seconds": 15,
        "max_output_tokens": 100,
        "max_tool_calls": 2,
        "repeats": 1,
        "tools": ["verify_word"],
        "corpus_id": "fixture-corpus",
        "provider": native_cursor.CURSOR_PROVIDER,
        "cursor_bin": binary,
    }
    config.update(extra)
    return config


def _stream_events() -> list[dict[str, Any]]:
    return [
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "login",
            "session_id": "fixture-session",
            "model": "Cursor Grok 4.6 High",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": '{"responses":{"opaque-1":"A"}}'}],
            },
            "session_id": "fixture-session",
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '{"responses":{"opaque-1":"A"}}',
            "session_id": "fixture-session",
            "usage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 0, "cacheWriteTokens": 0},
        },
    ]


def _fixture_cli(tmp_path: Path, *, stream: list[dict[str, Any]] | None = None) -> Path:
    output = stream or _stream_events()
    script = tmp_path / "cursor-agent-fixture"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json\n"
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "if '--version' in argv:\n"
        "    print('2026.09.10-fixture')\n"
        "elif '--help' in argv:\n"
        "    print('-p --print --model --output-format --trust --workspace --mode --approve-mcps')\n"
        "elif argv[:1] == ['status']:\n"
        "    print('✓ Logged in as fixture@example.com')\n"
        "else:\n"
        "    prompt = sys.stdin.read()\n"
        "    assert prompt, 'missing stdin prompt'\n"
        "    assert '-p' in argv and '--model' in argv and '--workspace' in argv\n"
        "    assert '--output-format' in argv and argv[argv.index('--output-format') + 1] == 'stream-json'\n"
        "    assert '--mode' in argv and argv[argv.index('--mode') + 1] == 'ask'\n"
        "    assert '-' not in argv  # must not pass literal '-' as prompt\n"
        f"    events = {json.dumps(output, ensure_ascii=False)!r}\n"
        "    for event in json.loads(events):\n"
        "        print(json.dumps(event, ensure_ascii=False, separators=(',', ':')))\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def test_native_validator_checks_model_and_binary_only() -> None:
    assert native_cursor.validate_cursor_config({"model": "cursor-grok-4.6-high"})["cursor_bin"] == "cursor-agent"
    with pytest.raises(native_cursor.CursorAdapterError, match="exact cursor-agent model id"):
        native_cursor.validate_cursor_config({"model": "Cursor Grok 4.6 High"})
    with pytest.raises(native_cursor.CursorAdapterError, match="adapter is not cursor"):
        native_cursor.validate_cursor_config({"adapter": "claude", "model": "cursor-grok-4.6-high"})


def test_full_validator_rejects_unknown_and_contradictory_provider(tmp_path: Path) -> None:
    binary = str(_fixture_cli(tmp_path))
    with pytest.raises(native_cursor.CursorAdapterError, match="unsupported fields"):
        native_cursor.validate_config(_config(binary, unexpected="value"))
    with pytest.raises(native_cursor.CursorAdapterError, match="provider identity is contradictory"):
        native_cursor.validate_config(_config(binary, provider="other-provider"))


def test_preflight_records_identity_and_unknown_effective_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = _fixture_cli(tmp_path)
    monkeypatch.setattr(native_cursor, "_assert_no_global_mcp", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USER", "fixture-user")
    (tmp_path / "home").mkdir()
    capability = native_cursor.preflight_cursor(_config(str(binary), tools=[]), "closed-book")
    assert capability["adapter"] == "cursor"
    assert capability["capability"] == "native-cursor-workspace-isolated"
    assert capability["requested_model"] == "cursor-grok-4.6-high"
    assert capability["cli_version"] == "2026.09.10-fixture"
    assert capability["effective_backend_model"] == "unknown"
    assert len(capability["binary_sha256"]) == 64


def test_preflight_rejects_global_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = _fixture_cli(tmp_path)
    mcp = tmp_path / "mcp.json"
    mcp.write_text(
        json.dumps({"mcpServers": {"other": {"url": "http://127.0.0.1:9/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(native_cursor, "_global_mcp_path", lambda: mcp)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USER", "fixture-user")
    (tmp_path / "home").mkdir()
    with pytest.raises(native_cursor.CursorAdapterError, match="global cursor MCP"):
        native_cursor.preflight_cursor(_config(str(binary), tools=[]), "closed-book")


def test_run_closed_book_trial_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = _fixture_cli(tmp_path)
    monkeypatch.setattr(native_cursor, "_assert_no_global_mcp", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USER", "fixture-user")
    (tmp_path / "home").mkdir()
    trial = native_cursor.run_cursor(
        _packet(),
        _config(str(binary), tools=[]),
        "closed-book",
        sources_url=None,
        prompt="answer as JSON",
    )
    assert trial["responses"] == {"opaque-1": "A"}
    assert trial["identity"]["adapter"] == "cursor"
    assert trial["identity"]["session_id"] == "fixture-session"
    assert trial["identity"]["api_key_source"] == "login"
    assert trial["identity"]["auth_path"] == "cli-login"
    assert trial["metrics"]["tool_calls"] == 0
    assert trial["metrics"]["input_tokens"] == 10


def test_run_rejects_api_key_auth_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = _stream_events()
    events[0]["apiKeySource"] = "env"
    binary = _fixture_cli(tmp_path, stream=events)
    monkeypatch.setattr(native_cursor, "_assert_no_global_mcp", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USER", "fixture-user")
    (tmp_path / "home").mkdir()
    with pytest.raises(native_cursor.CursorAdapterError, match="subscription login"):
        native_cursor.run_cursor(
            _packet(),
            _config(str(binary), tools=[]),
            "closed-book",
            sources_url=None,
            prompt="answer as JSON",
        )


def test_invalid_answer_preserves_candidate_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = _stream_events()
    events[1]["message"]["content"][0]["text"] = "not-json"
    events[2]["result"] = "not-json"
    binary = _fixture_cli(tmp_path, stream=events)
    monkeypatch.setattr(native_cursor, "_assert_no_global_mcp", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USER", "fixture-user")
    (tmp_path / "home").mkdir()
    trial = native_cursor.run_cursor(
        _packet(),
        _config(str(binary), tools=[]),
        "closed-book",
        sources_url=None,
        prompt="answer as JSON",
    )
    assert trial["status"] == "failed"
    assert trial["failure_reason"] == CANDIDATE_RESPONSE_ERROR
    assert trial["responses"] == {"opaque-1": None}
    assert is_candidate_response_failure(trial, expected_response_ids=["opaque-1"])


def test_sources_mirrors_proxy_and_counts_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "login",
            "session_id": "sources-session",
            "model": "Cursor Grok 4.6 High",
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Checking Sources"}]},
            "session_id": "sources-session",
        },
        {
            "type": "tool_call",
            "subtype": "started",
            "call_id": "call-1",
            "name": "mcp__sources__verify_word",
            "session_id": "sources-session",
        },
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-1",
            "name": "mcp__sources__verify_word",
            "session_id": "sources-session",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": '{"responses":{"opaque-1":"A"}}'}],
            },
            "session_id": "sources-session",
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": 'Checking Sources{"responses":{"opaque-1":"A"}}',
            "session_id": "sources-session",
            "usage": {"inputTokens": 11, "outputTokens": 4},
        },
    ]
    binary = _fixture_cli(tmp_path, stream=events)
    monkeypatch.setattr(native_cursor, "_assert_no_global_mcp", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USER", "fixture-user")
    (tmp_path / "home").mkdir()
    written: dict[str, Any] = {}

    original = native_cursor._write_sources_mcp

    def capture(workspace: Path, sources_url: str, tools: list[str], max_tool_calls: int) -> Path:
        path = original(workspace, sources_url, tools, max_tool_calls)
        written["payload"] = json.loads(path.read_text(encoding="utf-8"))
        written["argv_probe"] = True
        return path

    monkeypatch.setattr(native_cursor, "_write_sources_mcp", capture)
    trial = native_cursor.run_cursor(
        _packet(),
        _config(str(binary)),
        "sources",
        sources_url="http://127.0.0.1:8766/mcp",
        prompt="answer as JSON",
    )
    assert trial["responses"] == {"opaque-1": "A"}
    assert trial["metrics"]["tool_calls"] == 1
    servers = written["payload"]["mcpServers"]
    assert set(servers) == {"sources"}
    assert "mcp_proxy.py" in " ".join(servers["sources"]["args"])
    assert servers["sources"]["env"]["ZNO_NMT_SOURCES_URL"] == "http://127.0.0.1:8766/mcp"


def test_parser_counts_paired_tool_events_once() -> None:
    stdout = "\n".join(
        json.dumps(event, ensure_ascii=False)
        for event in [
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "login",
                "session_id": "s1",
                "model": "Cursor Grok 4.6 High",
            },
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": "c1",
                "name": "verify_word",
                "session_id": "s1",
            },
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": "c1",
                "name": "verify_word",
                "session_id": "s1",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"responses":{"opaque-1":"A"}}'}],
                },
                "session_id": "s1",
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": 'narration{"responses":{"opaque-1":"A"}}',
                "session_id": "s1",
            },
        ]
    )
    parsed = native_cursor._parse_stream_envelope(stdout, _packet(), {"verify_word"}, 1)
    assert parsed.tool_calls == 1
    assert parsed.responses == {"opaque-1": "A"}


def test_parser_rejects_foreign_session_and_incomplete_result() -> None:
    stdout = "\n".join(
        json.dumps(event, ensure_ascii=False)
        for event in [
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "login",
                "session_id": "s1",
                "model": "Cursor Grok 4.6 High",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"responses":{"opaque-1":"A"}}'}],
                },
                "session_id": "other",
            },
            {"type": "result", "session_id": "s1"},
        ]
    )
    with pytest.raises(native_cursor.CursorAdapterError, match="session identity"):
        native_cursor._parse_stream_envelope(stdout, _packet(), set(), 1)


def test_parser_rejects_missing_api_key_source() -> None:
    stdout = "\n".join(
        json.dumps(event, ensure_ascii=False)
        for event in [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "s1",
                "model": "Cursor Grok 4.6 High",
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "{}",
                "session_id": "s1",
            },
        ]
    )
    with pytest.raises(native_cursor.CursorAdapterError, match="subscription login"):
        native_cursor._parse_stream_envelope(stdout, _packet(), set(), 1)


def test_parser_rejects_unlisted_tool() -> None:
    stdout = "\n".join(
        json.dumps(event, ensure_ascii=False)
        for event in [
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "login",
                "session_id": "s1",
                "model": "Cursor Grok 4.6 High",
            },
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": "c1",
                "name": "Shell",
                "session_id": "s1",
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": '{"responses":{"opaque-1":"A"}}',
                "session_id": "s1",
            },
        ]
    )
    with pytest.raises(native_cursor.CursorAdapterError, match="tool_policy_error"):
        native_cursor._parse_stream_envelope(stdout, _packet(), {"verify_word"}, 2)


def test_build_argv_closed_book_omits_approve_mcps(tmp_path: Path) -> None:
    argv = native_cursor._build_argv("cursor-agent", "cursor-grok-4.6-high", tmp_path, approve_mcps=False)
    assert argv[0] == "cursor-agent"
    assert "-p" in argv
    assert "--approve-mcps" not in argv
    assert argv[argv.index("--mode") + 1] == "ask"
