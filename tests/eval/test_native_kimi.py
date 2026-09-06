"""Deterministic controls for the native Kimi adapter.

The fixture CLI is a local subprocess.  No provider credentials or network
calls are used by these tests.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ukrainian_llm_eval import native_kimi
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
        "adapter": "kimi",
        "model": "kimi-code/k2.5",
        "effort": "medium",
        "timeout_seconds": 15,
        "max_output_tokens": 100,
        "max_tool_calls": 2,
        "repeats": 1,
        "tools": ["verify_word"],
        "corpus_id": "fixture-corpus",
        "provider": native_kimi.KIMI_PROVIDER,
        "kimi_bin": binary,
    }
    config.update(extra)
    return config


def _fixture_cli(tmp_path: Path, *, stream: list[dict[str, Any]] | None = None) -> Path:
    output = stream or [
        {"role": "meta", "type": "system.version", "version": "0.41.0"},
        {"role": "assistant", "content": '{"responses":{"opaque-1":"A"}}'},
        {"role": "meta", "type": "session.resume_hint", "session_id": "fixture-session"},
    ]
    script = tmp_path / "kimi-fixture"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('0.41.0')\n"
        "elif '--help' in sys.argv:\n"
        "    print('--model --prompt --output-format stream-json --skills-dir --agent-file')\n"
        "else:\n"
        f"    events = {json.dumps(output, ensure_ascii=False)!r}\n"
        "    for event in json.loads(events):\n"
        "        print(json.dumps(event, ensure_ascii=False, separators=(',', ':')))\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def _provisioning(tmp_path: Path, *, model: str = "k2.5", effort: str = "medium") -> Path:
    root = tmp_path / "private-kimi"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    (root / "credentials").mkdir(mode=0o700)
    (root / "credentials").chmod(0o700)
    (root / "config.toml").write_text(
        "[providers.\"managed:kimi-code\"]\n"
        "type = \"kimi\"\n"
        "base_url = \"https://api.kimi.com/coding/v1\"\n"
        "api_key = \"\"\n\n"
        "[providers.\"managed:kimi-code\".oauth]\n"
        "storage = \"file\"\n"
        "key = \"oauth/kimi-code\"\n"
        "oauth_host = \"https://auth.kimi.com\"\n\n"
        f"[models.\"kimi-code/{model}\"]\n"
        "provider = \"managed:kimi-code\"\n"
        f"model = \"{model}\"\n"
        "max_context_size = 131072\n"
        "max_output_size = 256\n"
        "capabilities = [\"thinking\"]\n"
        "support_efforts = [\"low\", \"medium\"]\n"
        f"default_effort = \"{effort}\"\n",
        encoding="utf-8",
    )
    (root / "config.toml").chmod(0o600)
    (root / "credentials" / "kimi-code.json").write_text(
        json.dumps(
            {
                "access_token": "fixture-access-token",
                "refresh_token": "fixture-refresh-token",
                "expires_at": 4_000_000_000,
                "scope": "fixture",
                "token_type": "Bearer",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (root / "credentials" / "kimi-code.json").chmod(0o600)
    return root


def test_native_validator_checks_alias_and_binary_only() -> None:
    assert native_kimi.validate_kimi_config({"model": "kimi-code/k2.5"})["kimi_bin"] == "kimi"
    with pytest.raises(native_kimi.KimiAdapterError, match="exact kimi-code alias"):
        native_kimi.validate_kimi_config({"model": "k2.5"})
    with pytest.raises(native_kimi.KimiAdapterError, match="adapter is not kimi"):
        native_kimi.validate_kimi_config({"adapter": "claude", "model": "kimi-code/k2.5"})


def test_full_validator_rejects_unknown_and_contradictory_provider(tmp_path: Path) -> None:
    binary = str(_fixture_cli(tmp_path))
    with pytest.raises(native_kimi.KimiAdapterError, match="unsupported fields"):
        native_kimi.validate_config(_config(binary, unexpected="value"))
    with pytest.raises(native_kimi.KimiAdapterError, match="provider identity is contradictory"):
        native_kimi.validate_config(_config(binary, provider="other-provider"))


def test_missing_private_path_is_explicit_not_ready(tmp_path: Path) -> None:
    binary = str(_fixture_cli(tmp_path))
    with pytest.raises(native_kimi.KimiAdapterError, match="private_env_path is required"):
        native_kimi.preflight_kimi(_config(binary), "closed-book")


def test_preflight_records_installed_identity_and_unknown_effective_values(tmp_path: Path) -> None:
    binary = _fixture_cli(tmp_path)
    private = _provisioning(tmp_path)
    capability = native_kimi.preflight_kimi(
        _config(str(binary)),
        "closed-book",
        private_env_path=private,
    )
    assert capability["requested_model_alias"] == "kimi-code/k2.5"
    assert capability["effective_backend_model"] == "unknown"
    assert capability["account_identity"] == "unknown"
    assert capability["version_observed"] == "0.41.0"
    assert capability["supported_efforts"] == ["low", "medium"]
    assert capability["max_output_tokens_effective"] == "unknown"
    assert "fixture-access-token" not in json.dumps(capability)


def test_preflight_sources_checks_exact_proxy_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = _fixture_cli(tmp_path)
    private = _provisioning(tmp_path)
    monkeypatch.setattr(
        native_kimi.adapters,
        "_mcp_list_tools",
        lambda _url, _timeout: ([{"name": "verify_word", "inputSchema": {}}], "server-hash"),
    )
    capability = native_kimi.preflight_kimi(
        _config(str(binary)),
        "sources",
        "https://sources.example.test/mcp",
        private_env_path=private,
    )
    assert capability["mcp_server_identity_sha256"] == "server-hash"
    assert capability["tool_schema_sha256"] == native_kimi.adapters.digest(
        [{"name": "verify_word", "inputSchema": {}}]
    )


def test_runtime_tool_hash_is_configured_list_and_preflight_hash_is_live_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _fixture_cli(tmp_path)
    private = _provisioning(tmp_path)
    live_tools = [{"name": "verify_word", "inputSchema": {"type": "object"}}]
    monkeypatch.setattr(
        native_kimi.adapters,
        "_mcp_list_tools",
        lambda _url, _timeout: (live_tools, "server-hash"),
    )
    capability = native_kimi.preflight_kimi(
        _config(str(binary)),
        "sources",
        "https://sources.example.test/mcp",
        private_env_path=private,
    )
    result = native_kimi.run_kimi(
        _packet(),
        _config(str(binary)),
        "sources",
        sources_url="https://sources.example.test/mcp",
        prompt="Return only the requested JSON.",
        private_env_path=private,
    )

    assert capability["tool_schema_sha256"] == native_kimi.adapters.digest(live_tools)
    assert result["identity"]["configured_tools_sha256"] == native_kimi.adapters.digest(["verify_word"])
    assert result["identity"]["tool_schema_sha256"] is None
    assert result["identity"]["configured_tools_sha256"] != capability["tool_schema_sha256"]


def test_run_subprocess_fixture_matches_trial_shape_and_reports_unknown_usage(tmp_path: Path) -> None:
    binary = _fixture_cli(tmp_path)
    private = _provisioning(tmp_path)
    events: list[tuple[str, Any]] = []
    result = native_kimi.run_kimi(
        _packet(),
        _config(str(binary)),
        "closed-book",
        sources_url=None,
        prompt="Return only the requested JSON.",
        private_env_path=private,
        evidence=lambda kind, payload: events.append((kind, payload)),
    )
    assert result["responses"] == {"opaque-1": "A"}
    assert result["identity"]["session_id"] == "fixture-session"
    assert result["identity"]["effective_backend_model"] == "unknown"
    assert result["identity"]["configured_tools_sha256"] == native_kimi.adapters.digest([])
    assert result["identity"]["tool_schema_sha256"] == native_kimi.adapters.digest([])
    assert result["metrics"]["input_tokens"] is None
    assert result["metrics"]["output_tokens"] is None
    assert result["metrics"]["total_tokens"] is None
    assert result["metrics"]["cost_usd"] is None
    assert "status" not in result
    assert "failure_reason" not in result
    raw_result = dict(events)["cli_result"]
    assert "0.41.0" in raw_result["stdout"]
    assert "fixture-access-token" not in json.dumps(events)


def test_invalid_answer_after_verified_stream_preserves_identity_metrics_and_evidence(tmp_path: Path) -> None:
    answer_content = "```json\n{\"responses\":{\"opaque-1\":\"A\"}}\n```"
    stream = [
        {"role": "meta", "type": "system.version", "version": "0.41.0"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "type": "function",
                    "id": "tool-1",
                    "function": {"name": "mcp__sources__verify_word", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool-1", "content": "{}"},
        {
            "role": "assistant",
            "content": answer_content,
            "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7, "cost_usd": 0.01},
        },
        {"role": "meta", "type": "session.resume_hint", "session_id": "fixture-session"},
    ]
    binary = _fixture_cli(tmp_path, stream=stream)
    private = _provisioning(tmp_path)
    events: list[tuple[str, Any]] = []
    result = native_kimi.run_kimi(
        _packet(),
        _config(str(binary)),
        "sources",
        sources_url="https://sources.example.test/mcp",
        prompt="Return only the requested JSON.",
        private_env_path=private,
        evidence=lambda kind, payload: events.append((kind, payload)),
    )

    assert result["status"] == "failed"
    assert result["failure_reason"] == CANDIDATE_RESPONSE_ERROR
    assert result["responses"] == {"opaque-1": None}
    assert is_candidate_response_failure(result, expected_response_ids=["opaque-1"])
    assert result["identity"]["session_id"] == "fixture-session"
    assert result["identity"]["cli_version"] == "0.41.0"
    assert result["metrics"]["tool_calls"] == 1
    assert result["metrics"]["total_tokens"] == 7
    answer_event = dict(events)["candidate_answer_outcome"]
    assert answer_event["failure_reason"] == CANDIDATE_RESPONSE_ERROR
    assert answer_event["parser_reason"] == "CLI response is not JSON"
    assert answer_event["answer_content"] == answer_content
    assert answer_event["session_id"] == "fixture-session"
    assert answer_event["tool_calls"] == 1


def test_malformed_stream_envelope_cannot_be_admitted_as_task_failure() -> None:
    malformed = [
        {"role": "meta", "type": "system.version", "version": "0.41.0"},
        {"role": "assistant", "content": "```json\n{}\n```"},
    ]
    with pytest.raises(native_kimi.KimiAdapterError, match="session resume hint"):
        native_kimi._parse_stream_envelope(
            "\n".join(json.dumps(item) for item in malformed) + "\n",
            _packet(),
            set(),
            2,
            "kimi-code/k2.5",
            "0.41.0",
        )
    assert not is_candidate_response_failure(
        {
            "status": "failed",
            "failure_reason": CANDIDATE_RESPONSE_ERROR,
            "responses": {"opaque-1": None},
            "identity": {"session_id": None},
            "metrics": {"tool_calls": 0},
        },
        expected_response_ids=["opaque-1"],
    )


def test_run_uses_fresh_neutral_controls_and_exact_source_proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = _fixture_cli(tmp_path)
    private = _provisioning(tmp_path)
    observed: dict[str, Any] = {}
    monkeypatch.setattr(
        native_kimi,
        "_probe_cli",
        lambda requested, _timeout: native_kimi._CliProbe(
            requested,
            Path(requested),
            "b" * 64,
            "0.41.0",
        ),
    )

    def fake_process(argv: list[str], *, cwd: Path, env: dict[str, str], prompt: str, timeout: int, evidence: Any = None) -> subprocess.CompletedProcess[str]:
        observed.update(argv=argv, cwd=cwd, env=env, prompt=prompt, timeout=timeout)
        observed["profile"] = Path(argv[argv.index("--agent-file") + 1]).read_text(encoding="utf-8")
        observed["mcp"] = (Path(env[native_kimi.KIMI_HOME_ENV]) / "mcp.json").exists()
        return subprocess.CompletedProcess(argv, 0, "\n".join([
            json.dumps({"role": "meta", "type": "system.version", "version": "0.41.0"}),
            json.dumps({"role": "assistant", "content": '{"responses":{"opaque-1":"A"}}'}),
            json.dumps({"role": "meta", "type": "session.resume_hint", "session_id": "fake-session"}),
        ]) + "\n", "")

    monkeypatch.setattr(native_kimi, "_run_kimi_process", fake_process)
    result = native_kimi.run_kimi(
        _packet(),
        _config(str(binary)),
        "sources",
        sources_url="https://sources.example.test/mcp",
        prompt="prompt fixture",
        private_env_path=private,
    )
    assert result["responses"] == {"opaque-1": "A"}
    assert observed["prompt"] == "prompt fixture"
    assert observed["timeout"] == 15
    assert observed["argv"][observed["argv"].index("--model") + 1] == "kimi-code/k2.5"
    assert "-S" not in observed["argv"] and "-c" not in observed["argv"]
    assert observed["env"][native_kimi.KIMI_LEGACY_ENV] == "0"
    assert observed["env"][native_kimi.KIMI_EFFORT_ENV] == "medium"
    assert observed["env"][native_kimi.KIMI_OUTPUT_ENV] == "100"
    assert "OPENAI_API_KEY" not in observed["env"]
    assert observed["mcp"] is True
    assert 'tools: ["mcp__sources__verify_word"]' in observed["profile"]
    assert "${base_prompt}" not in observed["profile"]
    assert "${plugin_sections}" not in observed["profile"]
    assert "KIMI_CODE_HOME" in observed["env"]
    assert not observed["cwd"].joinpath(".git").exists()


def test_neutral_cwd_accepts_normal_empty_temp(tmp_path: Path) -> None:
    cwd = tmp_path / "empty"
    cwd.mkdir()

    native_kimi._assert_neutral_cwd(cwd)


def test_neutral_cwd_counts_dangling_control_symlink(tmp_path: Path) -> None:
    cwd = tmp_path / "empty"
    cwd.mkdir()
    (cwd / ".mcp.json").symlink_to(cwd / "missing-mcp.json")

    with pytest.raises(native_kimi.KimiAdapterError, match="ambient project controls"):
        native_kimi._assert_neutral_cwd(cwd)


def test_neutral_cwd_fails_closed_on_control_stat_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cwd = tmp_path / "empty"
    cwd.mkdir()
    blocked = cwd / ".git"
    original_lstat = Path.lstat

    def denied(path: Path) -> Any:
        if path == blocked:
            raise PermissionError("permission denied")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(native_kimi.KimiAdapterError, match="control check failed"):
        native_kimi._assert_neutral_cwd(cwd)


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ({"role": "meta", "type": "turn.step.retrying"}, "retry event"),
        ({"role": "meta", "type": "turn.ended"}, "meta event"),
        ({"role": "assistant", "tool_calls": [{"type": "function", "id": "1", "function": {"name": "Bash", "arguments": "{}"}}]}, "tool_policy"),
        ({"role": "assistant", "model": "kimi-code/other", "content": "{}"}, "model identity"),
        ({"role": "assistant", "fallback": True, "content": "{}"}, "fallback"),
    ],
)
def test_parser_rejects_retry_meta_tool_and_identity_violations(event: dict[str, Any], message: str) -> None:
    stream = [
        {"role": "meta", "type": "system.version", "version": "0.41.0"},
        event,
        {"role": "meta", "type": "session.resume_hint", "session_id": "fixture-session"},
    ]
    with pytest.raises(native_kimi.KimiAdapterError, match=message):
        native_kimi._parse_stream_json(
            "\n".join(json.dumps(item) for item in stream) + "\n",
            _packet(),
            set(),
            2,
            "kimi-code/k2.5",
            "0.41.0",
        )


def test_parser_rejects_missing_tool_result_and_accepts_usage_when_present() -> None:
    missing_result = [
        {"role": "meta", "type": "system.version", "version": "0.41.0"},
        {"role": "assistant", "tool_calls": [{"type": "function", "id": "1", "function": {"name": "mcp__sources__verify_word", "arguments": "{}"}}]},
        {"role": "assistant", "content": '{"responses":{"opaque-1":"A"}}'},
        {"role": "meta", "type": "session.resume_hint", "session_id": "fixture-session"},
    ]
    with pytest.raises(native_kimi.KimiAdapterError, match="tool result evidence"):
        native_kimi._parse_stream_json(
            "\n".join(json.dumps(item) for item in missing_result) + "\n",
            _packet(),
            {"mcp__sources__verify_word"},
            2,
            "kimi-code/k2.5",
            "0.41.0",
        )

    valid = [
        {"role": "meta", "type": "system.version", "version": "0.41.0"},
        {"role": "assistant", "tool_calls": [{"type": "function", "id": "1", "function": {"name": "mcp__sources__verify_word", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
        {"role": "assistant", "content": '{"responses":{"opaque-1":"A"}}', "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7, "cost_usd": 0}},
        {"role": "meta", "type": "session.resume_hint", "session_id": "fixture-session"},
    ]
    responses, session, version, calls, usage = native_kimi._parse_stream_json(
        "\n".join(json.dumps(item) for item in valid) + "\n",
        _packet(),
        {"mcp__sources__verify_word"},
        2,
        "kimi-code/k2.5",
        "0.41.0",
    )
    assert responses == {"opaque-1": "A"}
    assert session == "fixture-session"
    assert version == "0.41.0"
    assert calls == 1
    assert usage["total_tokens"] == 7


def test_private_provisioning_rejects_region_and_model_endpoint_drift(tmp_path: Path) -> None:
    binary = _fixture_cli(tmp_path)
    private = _provisioning(tmp_path)
    config_path = private / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'base_url = "https://api.kimi.com/coding/v1"',
            'base_url = "https://api.kimi.ai/coding/v1"',
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    with pytest.raises(native_kimi.KimiAdapterError, match="region configuration is contradictory"):
        native_kimi.preflight_kimi(_config(str(binary)), "closed-book", private_env_path=private)
