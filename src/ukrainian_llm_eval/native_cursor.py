"""Fail-closed native cursor-agent adapter (subscription login).

Runs headless ``cursor-agent`` with stdin prompts. Auth uses the operator's
Cursor subscription session (keychain / CLI login), not ``CURSOR_API_KEY`` and
not the Cursor SDK. Isolation is weaker than Codex: the CLI has no
``--ignore-user-config`` / ``--ephemeral`` / ``--deny-mcp``. Each attempt uses a
fresh empty ``--workspace``; closed-book omits MCP approval; Sources mirrors
only the allowlisted ``sources`` server via the shared MCP proxy. Global
``~/.cursor/mcp.json`` must be absent or empty or preflight fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import adapters
from .candidate_outcome import CANDIDATE_RESPONSE_ERROR
from .mcp_proxy import REFERENCE_TOOLS

CURSOR_ADAPTER = "cursor"
CURSOR_PROVIDER = "managed:cursor-subscription"
CURSOR_SCHEMA = "zno-nmt.config.v1"
CURSOR_CAPABILITY_SCHEMA = "zno-nmt.capability.v1"
CURSOR_HARNESS = "cursor-agent"
CURSOR_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SAFE_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]*$")
_MAX_STREAM_BYTES = 2_000_000
_MAX_VERSION_BYTES = 16_384
_MAX_HELP_BYTES = 256_000
_TOOL_POLICY_ERROR = "tool_policy_error"
_TOOL_LIMIT_ERROR = "tool_limit_error"
_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "schema",
        "adapter",
        "model",
        "effort",
        "timeout_seconds",
        "max_output_tokens",
        "max_tool_calls",
        "repeats",
        "tools",
        "corpus_id",
        "provider",
        "cursor_bin",
    }
)
_COMMON_CHILD_ENV = frozenset(
    {
        "PATH",
        "USER",
        "LOGNAME",
        "HOME",
        "TMPDIR",
        "SHELL",
        "TERM",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
_REQUIRED_HELP_FLAGS = frozenset(
    {
        "-p",
        "--print",
        "--model",
        "--output-format",
        "--trust",
        "--workspace",
        "--mode",
        "--approve-mcps",
    }
)


class CursorAdapterError(adapters.AdapterError):
    """A Cursor route or its isolation evidence violated the adapter contract."""


@dataclass(frozen=True)
class NativeCursorOptions:
    config: dict[str, Any]
    binary: str


@dataclass(frozen=True)
class _CliProbe:
    binary: str
    binary_path: Path
    binary_sha256: str
    version: str


@dataclass(frozen=True)
class _ParsedCursorStream:
    responses: dict[str, Any] | None
    session_id: str
    resolved_model: str
    api_key_source: str
    tool_calls: int
    usage: dict[str, int | float | None]
    answer_failure_reason: str | None
    answer_content: str | None


def _fail(message: str) -> CursorAdapterError:
    return CursorAdapterError(message)


def _require_exact_keys(value: Mapping[str, Any], allowed: set[str] | frozenset[str], label: str) -> None:
    unknown = set(value) - set(allowed)
    if unknown:
        raise _fail(f"{label} contains unsupported fields")


def _nonempty_text(value: Any, label: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > max_length:
        raise _fail(f"{label} must be a nonempty string")
    if not _SAFE_TEXT_RE.fullmatch(value):
        raise _fail(f"{label} contains control characters")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _fail(f"{label} must be positive")
    return value


def _validate_condition(condition: str, sources_url: str | None, tools: list[str]) -> None:
    if condition not in {"closed-book", "sources"}:
        raise _fail("condition must be closed-book or sources")
    if condition == "closed-book":
        if sources_url is not None:
            raise _fail("closed-book rejects sources URL")
        return
    if not tools or not isinstance(sources_url, str) or not sources_url.strip():
        raise _fail("sources condition requires nonempty sources URL and tools")


def validate_cursor_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate Cursor-specific fields without probing the CLI."""

    if not isinstance(config, Mapping):
        raise _fail("configuration must be an object")
    adapter = config.get("adapter")
    if adapter is not None and adapter != CURSOR_ADAPTER:
        raise _fail("configuration adapter is not cursor")
    model = _nonempty_text(config.get("model"), "configuration model")
    if not CURSOR_MODEL_RE.fullmatch(model):
        raise _fail("configuration model must be an exact cursor-agent model id")
    binary = config.get("cursor_bin", "cursor-agent")
    binary = _nonempty_text(binary, "configuration cursor_bin", max_length=1024)
    return {**dict(config), "model": model, "cursor_bin": binary}


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the full serialisable Cursor route."""

    checked = validate_cursor_config(config)
    _require_exact_keys(checked, _ALLOWED_CONFIG_KEYS, "configuration")
    if checked.get("schema") != CURSOR_SCHEMA:
        raise _fail("configuration schema mismatch")
    if checked.get("adapter") != CURSOR_ADAPTER:
        raise _fail("configuration adapter is not cursor")
    effort = checked.get("effort")
    if effort is not None:
        effort = _nonempty_text(effort, "configuration effort", max_length=64)
        checked["effort"] = effort
    for field in ("timeout_seconds", "max_output_tokens", "max_tool_calls", "repeats"):
        checked[field] = _positive_int(checked.get(field), f"configuration {field}")
    tools = checked.get("tools")
    if not isinstance(tools, list) or any(
        not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", tool) or tool not in REFERENCE_TOOLS
        for tool in tools
    ):
        raise _fail("configuration tools must be allowlisted Sources MCP references")
    if len(tools) != len(set(tools)):
        raise _fail("configuration tools contains duplicates")
    corpus_id = checked.get("corpus_id")
    if corpus_id is not None:
        checked["corpus_id"] = _nonempty_text(corpus_id, "configuration corpus_id", max_length=256)
    provider = checked.get("provider")
    if provider is None:
        checked["provider"] = CURSOR_PROVIDER
    elif provider != CURSOR_PROVIDER:
        raise _fail("provider identity is contradictory")
    return checked


def validate_options(config: Mapping[str, Any]) -> NativeCursorOptions:
    checked = validate_config(config)
    return NativeCursorOptions(config=checked, binary=checked["cursor_bin"])


def _resolve_binary(configured: str) -> Path:
    if configured in {"cursor-agent", "agent"}:
        found = shutil.which("cursor-agent") or (shutil.which("agent") if configured == "agent" else None)
        if not found:
            raise _fail("cursor-agent CLI unavailable")
        path = Path(found)
    else:
        path = Path(configured)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise _fail("cursor-agent CLI unavailable")
    resolved = path.resolve()
    if resolved.name == "agent" and shutil.which("cursor-agent"):
        # Prefer the unambiguous binary when an ambiguous agent name was resolved.
        preferred = Path(shutil.which("cursor-agent")).resolve()
        return preferred
    return resolved


def _probe_cli(binary_name: str, timeout: int) -> _CliProbe:
    binary_path = _resolve_binary(binary_name)
    try:
        version_proc = subprocess.run(
            [str(binary_path), "--version"],
            capture_output=True,
            text=True,
            timeout=min(15, timeout),
            check=False,
        )
        help_proc = subprocess.run(
            [str(binary_path), "--help"],
            capture_output=True,
            text=True,
            timeout=min(15, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail("cursor-agent CLI capability unavailable") from exc
    version_out = (version_proc.stdout or "") + (version_proc.stderr or "")
    help_out = (help_proc.stdout or "") + (help_proc.stderr or "")
    if version_proc.returncode != 0 or len(version_out.encode()) > _MAX_VERSION_BYTES:
        raise _fail("cursor-agent CLI version unavailable")
    if help_proc.returncode != 0 or len(help_out.encode()) > _MAX_HELP_BYTES:
        raise _fail("cursor-agent CLI capability unavailable")
    missing = [flag for flag in _REQUIRED_HELP_FLAGS if flag not in help_out]
    if missing:
        raise _fail("cursor-agent CLI capability unavailable")
    version = version_out.strip().splitlines()[0][:160] if version_out.strip() else "unknown"
    return _CliProbe(
        binary=str(binary_path),
        binary_path=binary_path,
        binary_sha256=hashlib.sha256(binary_path.read_bytes()).hexdigest(),
        version=version,
    )


def _assert_login(binary: str, timeout: int) -> None:
    try:
        result = subprocess.run(
            [binary, "status"],
            capture_output=True,
            text=True,
            timeout=min(15, timeout),
            check=False,
            env=_child_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail("cursor-agent subscription login unavailable") from exc
    text = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
    if result.returncode != 0 or "not logged in" in text or "logged in" not in text:
        raise _fail("cursor-agent subscription login unavailable")


def _global_mcp_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def _assert_no_global_mcp() -> None:
    path = _global_mcp_path()
    try:
        if not path.exists():
            return
        if path.is_symlink():
            raise _fail("global cursor MCP config must not be a symlink")
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail("global cursor MCP config unavailable") from exc
    if not raw.strip():
        return
    try:
        payload = adapters._strict_json_loads(raw)
    except adapters.AdapterError as exc:
        raise _fail("global cursor MCP config is invalid") from exc
    if not isinstance(payload, Mapping):
        raise _fail("global cursor MCP config is invalid")
    servers = payload.get("mcpServers")
    if servers in (None, {}):
        return
    if isinstance(servers, Mapping) and len(servers) == 0:
        return
    raise _fail("global cursor MCP config must be empty for isolated evaluation")


def _settings_sha256(*, condition: str, tools: list[str], max_tool_calls: int) -> str:
    return adapters.digest(
        {
            "mode": "ask",
            "output_format": "stream-json",
            "trust": True,
            "approve_mcps": condition == "sources",
            "global_mcp_denied": True,
            "tools": tools if condition == "sources" else [],
            "max_tool_calls": max_tool_calls,
            "prompt_delivery": "stdin",
            "isolation": "workspace-only",
        }
    )


def _request_shape_sha256(model: str, condition: str) -> str:
    return adapters.digest(
        {
            "argv": [
                "cursor-agent",
                "-p",
                "--model",
                model,
                "--output-format",
                "stream-json",
                "--trust",
                "--workspace",
                "<ephemeral>",
                "--mode",
                "ask",
                *(["--approve-mcps"] if condition == "sources" else []),
            ],
            "stdin": "prompt",
        }
    )


def preflight_cursor(
    config: Mapping[str, Any],
    condition: str,
    sources_url: str | None = None,
) -> dict[str, Any]:
    options = validate_options(config)
    checked = options.config
    _validate_condition(condition, sources_url, checked["tools"])
    _assert_no_global_mcp()
    probe = _probe_cli(options.binary, checked["timeout_seconds"])
    _assert_login(probe.binary, checked["timeout_seconds"])
    tools, identity = (
        adapters._mcp_list_tools(str(sources_url), checked["timeout_seconds"])
        if condition == "sources"
        else ([], None)
    )
    if condition == "sources" and not set(checked["tools"]) <= {tool["name"] for tool in tools}:
        raise _fail("Sources MCP does not expose configured tools")
    return {
        "schema": CURSOR_CAPABILITY_SCHEMA,
        "adapter": CURSOR_ADAPTER,
        "condition": condition,
        "capability": "native-cursor-workspace-isolated",
        "requested_model": checked["model"],
        "requested_model_alias": checked["model"],
        "requested_effort": checked.get("effort"),
        "accepted_effort": "unknown",
        "effective_backend_model": "unknown",
        "account_identity": "unknown",
        "cli_version": probe.version,
        "binary_sha256": probe.binary_sha256,
        "native_config_sha256": adapters.digest(
            {key: checked[key] for key in sorted(_ALLOWED_CONFIG_KEYS) if key in checked}
        ),
        "catalog_provider_sha256": adapters.digest(CURSOR_PROVIDER),
        "catalog_model_sha256": adapters.digest(checked["model"]),
        "settings_sha256": _settings_sha256(
            condition=condition, tools=checked["tools"], max_tool_calls=checked["max_tool_calls"]
        ),
        "request_shape_sha256": _request_shape_sha256(checked["model"], condition),
        "tools_sha256": adapters.digest(checked["tools"] if condition == "sources" else []),
        "tool_schema_sha256": adapters.digest(tools),
        "mcp_server_identity_sha256": identity,
        "corpus_id_sha256": adapters.digest(checked["corpus_id"]) if checked["corpus_id"] else None,
        "max_output_tokens_effective": "unknown",
    }


preflight = preflight_cursor


def _assert_neutral_workspace(workspace: Path) -> None:
    """Reject ambient project controls in the workspace or any ancestor."""

    current = workspace.resolve()
    while True:
        for name in (".git", ".cursor/rules", ".cursor/mcp.json"):
            path = current / name
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _fail("isolated cursor workspace control check failed") from exc
            raise _fail("isolated cursor workspace has ambient project controls")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _write_sources_mcp(workspace: Path, sources_url: str, tools: list[str], max_tool_calls: int) -> Path:
    proxy = Path(__file__).with_name("mcp_proxy.py").resolve()
    if not proxy.is_file() or not Path(sys.executable).is_file():
        raise _fail("Sources MCP proxy unavailable")
    cursor_dir = workspace / ".cursor"
    try:
        cursor_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = cursor_dir / "mcp.json"
        payload = {
            "mcpServers": {
                "sources": {
                    "command": str(Path(sys.executable).resolve()),
                    "args": [
                        str(proxy),
                        "--url-env",
                        "ZNO_NMT_SOURCES_URL",
                        "--tools",
                        json.dumps(tools, ensure_ascii=False, separators=(",", ":")),
                        "--max-tool-calls",
                        str(max_tool_calls),
                    ],
                    "env": {"ZNO_NMT_SOURCES_URL": sources_url},
                }
            }
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        raise _fail("isolated Sources MCP config could not be written") from exc
    return path


def _child_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _COMMON_CHILD_ENV and value}
    if "HOME" not in env or "USER" not in env:
        raise _fail("cursor-agent subscription identity environment unavailable")
    # Never forward API-key auth for this subscription route.
    return env


def _build_argv(binary: str, model: str, workspace: Path, *, approve_mcps: bool) -> list[str]:
    argv = [
        binary,
        "-p",
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--trust",
        "--workspace",
        str(workspace),
        "--mode",
        "ask",
    ]
    if approve_mcps:
        argv.append("--approve-mcps")
    return argv


def _run_cursor_process(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    prompt: str,
    timeout: int,
    evidence: Callable[[str, Any], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (NameError, ProcessLookupError, OSError):
            if "process" in locals():
                process.kill()
        if "process" in locals():
            stdout, stderr = process.communicate()
            if evidence is not None:
                evidence("cli_timeout", {"stdout": stdout, "stderr": stderr, "returncode": process.returncode})
        raise _fail("cursor-agent CLI timeout") from exc
    except OSError as exc:
        raise _fail("cursor-agent CLI invocation failed") from exc
    return subprocess.CompletedProcess(argv, process.returncode, stdout=stdout, stderr=stderr)


def _usage_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return value


def _assistant_text(event: Mapping[str, Any]) -> str | None:
    message = event.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "".join(parts)
        if isinstance(content, str):
            return content
    result = event.get("result")
    if isinstance(result, str):
        return result
    return None


def _tool_call_id(event: Mapping[str, Any]) -> str | None:
    for key in ("call_id", "callId", "id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool_call = event.get("tool_call")
    if isinstance(tool_call, Mapping):
        for key in ("call_id", "callId", "id", "toolCallId"):
            value = tool_call.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for payload in tool_call.values():
            if isinstance(payload, Mapping):
                args = payload.get("args")
                if isinstance(args, Mapping):
                    for key in ("toolCallId", "call_id", "id"):
                        value = args.get(key)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
    return None


def _tool_name(event: Mapping[str, Any]) -> str | None:
    for key in ("name", "toolName", "tool_name"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool_call = event.get("tool_call")
    if isinstance(tool_call, Mapping):
        for key in ("name", "toolName", "tool_name"):
            value = tool_call.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        function = tool_call.get("function")
        if isinstance(function, Mapping) and isinstance(function.get("name"), str):
            return function["name"].strip()
        # Cursor native shapes: {"readToolCall": {...}}, {"mcpToolCall": {"name": ...}}
        for key, payload in tool_call.items():
            if not isinstance(key, str):
                continue
            if key.endswith("ToolCall") and key != "toolCall":
                if isinstance(payload, Mapping):
                    nested_name = payload.get("name")
                    if isinstance(nested_name, str) and nested_name.strip():
                        return nested_name.strip()
                    # mcp__sources__verify_word style may live under server/tool fields
                    for nested_key in ("toolName", "tool", "serverToolName"):
                        nested = payload.get(nested_key)
                        if isinstance(nested, str) and nested.strip():
                            return nested.strip()
                # Fall back to the camelCase tool family name for policy checks
                # after normalization; MCP sources tools must still expose a name.
                return key[: -len("ToolCall")]
    message = event.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, Mapping):
                    continue
                if item.get("type") in {"tool_use", "tool_call"} and isinstance(item.get("name"), str):
                    return item["name"].strip()
    return None


def _normalize_tool_name(name: str) -> str:
    # Cursor may prefix MCP tools; accept bare reference names and mcp__sources__* forms.
    if name.startswith("mcp__sources__"):
        return name[len("mcp__sources__") :]
    if name.startswith("sources:"):
        return name.split(":", 1)[1]
    if "/" in name:
        return name.rsplit("/", 1)[-1]
    if name.endswith("ToolCall"):
        return name[: -len("ToolCall")]
    return name


def _check_session(event: Mapping[str, Any], session_id: str) -> None:
    sid = event.get("session_id")
    if sid is None:
        raise _fail("CLI session identity missing")
    if not isinstance(sid, str) or sid.strip() != session_id:
        raise _fail("CLI session identity drift")


def _parse_answer_payload(content: str, packet: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        parsed = adapters._strict_json_loads(content)
        return adapters._extract_responses(parsed, packet)
    except adapters.AdapterError:
        return None


def _parse_stream_envelope(
    stdout: str,
    packet: Mapping[str, Any],
    allowed_tools: set[str],
    max_tools: int,
) -> _ParsedCursorStream:
    if len(stdout.encode("utf-8", errors="strict")) > _MAX_STREAM_BYTES:
        raise _fail("cursor-agent CLI output exceeds limit")
    session_id: str | None = None
    resolved_model = "unknown"
    api_key_source = "unknown"
    answer_segments: list[str] = []
    seen_tool_ids: set[str] = set()
    tool_calls = 0
    usage: dict[str, int | float | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
    }
    saw_init = False
    saw_result = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = adapters._strict_json_loads(line)
        except adapters.AdapterError as exc:
            raise _fail("CLI emitted invalid stream JSON") from exc
        if not isinstance(event, Mapping):
            raise _fail("CLI emitted invalid stream event")
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            if saw_init:
                raise _fail("CLI emitted duplicate init event")
            saw_init = True
            session_id = _nonempty_text(event.get("session_id"), "CLI session id", max_length=256)
            model = event.get("model")
            if isinstance(model, str) and model.strip():
                resolved_model = model.strip()
            source = event.get("apiKeySource")
            if not isinstance(source, str) or not source.strip():
                raise _fail("cursor-agent auth source is not subscription login")
            api_key_source = source.strip()
            if api_key_source != "login":
                # Subscription route must not silently switch to env/flag API keys
                # and must not claim cli-login without positive login evidence.
                raise _fail("cursor-agent auth source is not subscription login")
            continue
        if session_id is None:
            raise _fail("CLI stream envelope incomplete")
        if event_type in {"user", "thinking"}:
            _check_session(event, session_id)
            continue
        if event_type == "tool_call":
            _check_session(event, session_id)
            subtype = event.get("subtype")
            call_id = _tool_call_id(event)
            name = _tool_name(event)
            if name is None:
                raise _fail("CLI tool call surface is malformed")
            normalized = _normalize_tool_name(name)
            if normalized not in allowed_tools and name not in allowed_tools:
                raise _fail(_TOOL_POLICY_ERROR)
            if subtype == "completed":
                if call_id is None:
                    raise _fail("CLI tool call id is missing")
                if call_id not in seen_tool_ids:
                    raise _fail("CLI tool call completion without start")
                continue
            if subtype not in {None, "started"}:
                raise _fail("CLI tool call surface is malformed")
            if call_id is None:
                raise _fail("CLI tool call id is missing")
            if call_id in seen_tool_ids:
                raise _fail("CLI emitted duplicate tool call id")
            seen_tool_ids.add(call_id)
            tool_calls += 1
            if tool_calls > max_tools:
                raise _fail(_TOOL_LIMIT_ERROR)
            continue
        if event_type == "assistant":
            _check_session(event, session_id)
            # Reject tool-bearing assistant shapes; Cursor documents tools as
            # type=tool_call events. Counting them here would reopen policy holes.
            if event.get("subtype") == "tool_call" or _tool_name(event) is not None:
                raise _fail("CLI tool call surface is malformed")
            # Skip partial-stream duplicates when present.
            if event.get("model_call_id") is not None:
                continue
            text = _assistant_text(event)
            if text is not None and text.strip():
                answer_segments.append(text)
            continue
        if event_type == "result":
            if saw_result:
                raise _fail("CLI emitted duplicate result event")
            _check_session(event, session_id)
            if event.get("subtype") != "success" or event.get("is_error") is not False:
                raise _fail("cursor-agent CLI result error")
            if "result" not in event or not isinstance(event.get("result"), str):
                raise _fail("CLI stream envelope incomplete")
            saw_result = True
            raw_usage = event.get("usage")
            if isinstance(raw_usage, Mapping):
                usage["input_tokens"] = _usage_number(raw_usage.get("inputTokens", raw_usage.get("input_tokens")))
                usage["output_tokens"] = _usage_number(raw_usage.get("outputTokens", raw_usage.get("output_tokens")))
                total = raw_usage.get("totalTokens", raw_usage.get("total_tokens"))
                if total is None and usage["input_tokens"] is not None and usage["output_tokens"] is not None:
                    total = usage["input_tokens"] + usage["output_tokens"]
                usage["total_tokens"] = _usage_number(total)
            # Do not use concatenated result text as the answer; prefer the final
            # assistant segment after tools (Cursor result is all segments joined).
            continue
        if event_type is not None:
            # Unknown event types are ignored only after session check when they carry one.
            if "session_id" in event:
                _check_session(event, session_id)
            continue
    if not saw_init or not saw_result or not session_id:
        raise _fail("CLI stream envelope incomplete")
    if not allowed_tools and tool_calls:
        raise _fail(_TOOL_POLICY_ERROR)
    answer_content = answer_segments[-1] if answer_segments else None
    responses = None
    answer_failure_reason = None
    if answer_content is None:
        answer_failure_reason = CANDIDATE_RESPONSE_ERROR
    else:
        responses = _parse_answer_payload(answer_content, packet)
        if responses is None:
            answer_failure_reason = CANDIDATE_RESPONSE_ERROR
    return _ParsedCursorStream(
        responses=responses,
        session_id=session_id,
        resolved_model=resolved_model,
        api_key_source=api_key_source,
        tool_calls=tool_calls,
        usage=usage,
        answer_failure_reason=answer_failure_reason,
        answer_content=answer_content,
    )


def _identity(
    checked: Mapping[str, Any],
    probe: _CliProbe,
    parsed: _ParsedCursorStream,
    *,
    condition: str,
) -> dict[str, Any]:
    return {
        "adapter": CURSOR_ADAPTER,
        "harness": CURSOR_HARNESS,
        "model": checked["model"],
        "provider": CURSOR_PROVIDER,
        "account_identity": "unknown",
        "session_id": parsed.session_id,
        "requested_model": checked["model"],
        "requested_model_alias": checked["model"],
        "effective_model": "unknown",
        "effective_backend_model": "unknown",
        "resolved_model": parsed.resolved_model,
        "requested_effort": checked.get("effort"),
        "accepted_effort": "unknown",
        "effective_effort": "unknown",
        "cli_version": probe.version,
        "version_observed": probe.version,
        "binary_sha256": probe.binary_sha256,
        "native_config_sha256": adapters.digest(
            {key: checked[key] for key in sorted(_ALLOWED_CONFIG_KEYS) if key in checked}
        ),
        "catalog_provider_sha256": adapters.digest(CURSOR_PROVIDER),
        "catalog_model_sha256": adapters.digest(checked["model"]),
        "configured_tools_sha256": adapters.digest(checked["tools"] if condition == "sources" else []),
        "tool_schema_sha256": None if condition == "sources" else adapters.digest([]),
        "corpus_id_sha256": adapters.digest(checked["corpus_id"]) if checked["corpus_id"] else None,
        "mcp_server_identity_sha256": None,
        "max_output_tokens_configured": checked["max_output_tokens"],
        "max_output_tokens_effective": "unknown",
        "settings_sha256": _settings_sha256(
            condition=condition, tools=checked["tools"], max_tool_calls=checked["max_tool_calls"]
        ),
        "request_shape_sha256": _request_shape_sha256(checked["model"], condition),
        "api_key_source": parsed.api_key_source,
        "auth_path": "cli-login",
    }


def run_cursor(
    packet: Mapping[str, Any],
    config: Mapping[str, Any],
    condition: str,
    *,
    sources_url: str | None = None,
    prompt: str,
    evidence: Callable[[str, Any], None] | None = None,
) -> dict[str, Any]:
    options = validate_options(config)
    checked = options.config
    _validate_condition(condition, sources_url, checked["tools"])
    _assert_no_global_mcp()
    probe = _probe_cli(options.binary, checked["timeout_seconds"])
    _assert_login(probe.binary, checked["timeout_seconds"])
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ukrainian-llm-eval-cursor-") as temp:
        workspace = Path(temp) / "workspace"
        workspace.mkdir(mode=0o700)
        _assert_neutral_workspace(workspace)
        if condition == "sources":
            _write_sources_mcp(workspace, str(sources_url), checked["tools"], checked["max_tool_calls"])
        argv = _build_argv(
            probe.binary,
            checked["model"],
            workspace,
            approve_mcps=condition == "sources",
        )
        env = _child_env()
        if evidence is not None:
            evidence(
                "cli_invocation",
                {
                    "argv": argv,
                    "cwd": str(workspace),
                    "prompt_bytes": len(prompt.encode("utf-8")),
                },
            )
        result = _run_cursor_process(
            argv,
            cwd=workspace,
            env=env,
            prompt=prompt,
            timeout=checked["timeout_seconds"],
            evidence=evidence,
        )
        if evidence is not None:
            evidence(
                "cli_result",
                {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
            )
        if result.returncode != 0:
            raise _fail("cursor-agent CLI failed")
        allowed = set(checked["tools"]) if condition == "sources" else set()
        parsed = _parse_stream_envelope(
            result.stdout or "",
            packet,
            allowed,
            checked["max_tool_calls"],
        )
        identity = _identity(checked, probe, parsed, condition=condition)
        metrics = {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "input_tokens": parsed.usage["input_tokens"],
            "output_tokens": parsed.usage["output_tokens"],
            "total_tokens": parsed.usage["total_tokens"],
            "cost_usd": parsed.usage["cost_usd"],
            "tool_calls": parsed.tool_calls,
        }
        if parsed.answer_failure_reason is not None:
            if evidence is not None:
                evidence(
                    "candidate_answer_outcome",
                    {
                        "failure_reason": parsed.answer_failure_reason,
                        "answer_content": parsed.answer_content,
                    },
                )
            return {
                "status": "failed",
                "failure_reason": parsed.answer_failure_reason,
                "responses": {str(item["id"]): None for item in packet["items"]},
                "identity": identity,
                "metrics": metrics,
            }
        return {
            "responses": parsed.responses,
            "identity": identity,
            "metrics": metrics,
        }


run = run_cursor


__all__ = [
    "CURSOR_ADAPTER",
    "CURSOR_HARNESS",
    "CURSOR_PROVIDER",
    "CursorAdapterError",
    "NativeCursorOptions",
    "preflight",
    "preflight_cursor",
    "run",
    "run_cursor",
    "validate_config",
    "validate_cursor_config",
    "validate_options",
]
