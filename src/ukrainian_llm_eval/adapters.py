"""Narrow, fail-closed provider adapters for the ZNO/NMT evaluator.

These adapters intentionally expose only an exam-response contract.  They do
not accept a grading key, execute model supplied commands, or retain provider
transcripts in the returned receipt.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import functools
import hashlib
import json
import math
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .gec import GEC_PACKET_SCHEMA
from .mcp_proxy import physical_lines as _physical_lines
from .mcp_proxy import REFERENCE_TOOLS, SMOKE_CATALOG_SHA256 as SMOKE_CATALOG_SHA256, SMOKE_TOOLS as SMOKE_TOOLS, normalized_catalog


class AdapterError(ValueError):
    """A provider transport or its isolation evidence violated this contract."""


class AuthUnavailable(AdapterError):
    """Staged native authentication cannot be used; no candidate was run."""


_ATTEMPT = contextvars.ContextVar("native_evaluation_attempt", default=None)
_NATIVE_ENV = frozenset({"PATH", "LANG", "LC_ALL", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR"})
METADATA_LIMIT = 8
CLAUDE_AUTH_CONTRACT = "4503bfe11a6c7fcc1e0b39b5e0d347c04248f750b03b0977b3ad6b531fe6f358"


def _private_directory_fd(path: Path, *, owner_only: bool = True) -> int:
    """Walk absolute trusted ancestors with directory-relative no-follow opens."""
    if not path.is_absolute() or any(part in {"..", "."} for part in path.parts):
        raise AdapterError("private directory boundary invalid") from None
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            if st.st_uid not in {0, os.getuid()}:
                raise AdapterError("private directory owner invalid")
            # A root-owned sticky temporary ancestor is a supported trust boundary.
            if st.st_mode & 0o022 and not (st.st_uid == 0 and st.st_mode & stat.S_ISVTX):
                raise AdapterError("private directory ancestor writable")
            if owner_only and index == len(path.parts) - 2:
                if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
                    raise AdapterError("private directory must be current-user 0700")
        return fd
    except (OSError, AdapterError):
        os.close(fd)
        raise AdapterError("private directory unavailable or unsafe") from None


def _checked_private_read(root: Path, relative: str, *, limit: int, mode: int | None = 0o600,
                          allow_empty: bool = False, system_file: bool = False) -> bytes:
    """Bound same-FD reads, checking the object and its directory entry afterward."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {"..", "."} for p in parts):
        raise AdapterError("private artifact boundary invalid")
    parent = _private_directory_fd(root, owner_only=not system_file)
    root_identity = os.fstat(parent)
    fd = None
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
            st = os.fstat(parent)
            if st.st_uid not in ({0, os.getuid()} if system_file else {os.getuid()}) or st.st_mode & 0o022:
                raise AdapterError("private artifact directory unsafe")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in ({0, os.getuid()} if system_file else {os.getuid()}) or before.st_nlink != 1
                or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
                or (mode is None and before.st_mode & (0o022 if system_file else 0o077))
                or not (0 if allow_empty else 1) <= before.st_size <= limit):
            raise AdapterError("private artifact type, owner, mode or size invalid")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise AdapterError("private artifact exceeds bound")
        after = os.fstat(fd)
        entry = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        def signature(st):
            return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                    st.st_nlink, st.st_uid, st.st_mode)
        if signature(before) != signature(after) or signature(after) != signature(entry) or size != before.st_size:
            raise AdapterError("private artifact mutated during capture")
        checked_root = _private_directory_fd(root, owner_only=not system_file)
        try:
            current_root = os.fstat(checked_root)
            if (root_identity.st_dev, root_identity.st_ino) != (current_root.st_dev, current_root.st_ino):
                raise AdapterError("private artifact root changed")
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=checked_root)
                os.close(checked_root)
                checked_root = child
                st = os.fstat(checked_root)
                if st.st_uid not in ({0, os.getuid()} if system_file else {os.getuid()}) or st.st_mode & 0o022:
                    raise AdapterError("private artifact directory changed")
            current_parent = os.fstat(checked_root)
            old_parent = os.fstat(parent)
            current_entry = os.stat(parts[-1], dir_fd=checked_root, follow_symlinks=False)
            if (current_parent.st_dev, current_parent.st_ino) != (old_parent.st_dev, old_parent.st_ino) or signature(current_entry) != signature(after):
                raise AdapterError("private artifact parent changed")
        finally:
            os.close(checked_root)
        return b"".join(chunks)
    except (OSError, AdapterError):
        raise AdapterError("private artifact unavailable or unsafe") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _exclusive_private_write(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def _isolated_environment(home: Path, root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _NATIVE_ENV}
    for key, relative in (("HOME", "home"), ("TMPDIR", "tmp"), ("XDG_CONFIG_HOME", "config"),
                          ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        directory = home if key == "HOME" else root / relative
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        env[key] = str(directory)
    return env


@contextlib.contextmanager
def _native_attempt():
    """One root per top-level invocation; nested adapter setup shares its boundary."""
    active = _ATTEMPT.get()
    if active is not None:
        yield active
        return
    # A managed lease can expose a group-writable payload subdirectory beneath
    # its private root. Allocate under the nearest checked private lease parent.
    temporary_parent = Path(tempfile.gettempdir())
    while True:
        try:
            fd = _private_directory_fd(temporary_parent, owner_only=False)
        except AdapterError:
            if temporary_parent.parent == temporary_parent:
                raise
            temporary_parent = temporary_parent.parent
        else:
            os.close(fd)
            break
    with tempfile.TemporaryDirectory(prefix="ukrainian-eval-attempt-", dir=temporary_parent) as temp:
        root = Path(temp)
        root.chmod(0o700)
        fd = _private_directory_fd(root)
        os.close(fd)
        home, cwd = root / "home", root / "workspace"
        cwd.mkdir(mode=0o700)
        env = _isolated_environment(home, root)
        codex_home = home / "codex-home"
        codex_home.mkdir(mode=0o700)
        env["CODEX_HOME"] = str(codex_home)
        attempt = {"root": root, "home": home, "cwd": cwd, "env": env, "id": uuid.uuid4().hex}
        token = _ATTEMPT.set(attempt)
        try:
            yield attempt
        finally:
            _ATTEMPT.reset(token)


def _isolated_native(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with _native_attempt() as attempt:
            evidence = kwargs.get("evidence")
            if evidence is not None:
                evidence("native_isolation", {"attempt_id": attempt["id"], "fresh_root": True,
                    "directories": ["home", "workspace", "tmp", "config", "data", "cache", "state"],
                    "environment_keys": sorted(attempt["env"]), "managed_inventory": _managed_inventory()})
            for value in (*args, *kwargs.values()):
                if isinstance(value, Mapping) and "timeout_seconds" in value and "deadline" not in attempt:
                    attempt["deadline"] = time.monotonic() + value["timeout_seconds"]
            return function(*args, **kwargs)
    return wrapped


def _remaining_native_deadline(default: float) -> float:
    attempt = _ATTEMPT.get()
    remaining = min(default, attempt.get("deadline", time.monotonic() + default) - time.monotonic())
    if remaining <= 0:
        raise AdapterError("timeout before candidate execution")
    return remaining


def _managed_inventory() -> dict[str, Any]:
    """Privately detect mandatory host settings; expose only presence and digests."""
    results = {}
    for label, path in (("claude_settings", Path("/etc/claude-code/managed-settings.json")),
                        ("claude_mcp", Path("/etc/claude-code/managed-mcp.json"))):
        try:
            raw = _checked_private_read(path.parent, path.name, limit=1_000_000, mode=None, system_file=True)
            results[label] = {"present": True, "sha256": digest(raw)}
        except AdapterError:
            try:
                path.lstat()
            except FileNotFoundError:
                results[label] = {"present": False, "sha256": None}
            except OSError:
                results[label] = {"present": "unknown", "sha256": None}
            else:
                results[label] = {"present": True, "sha256": None}
    results["other_native_managed_inventory"] = "unknown"
    return results


def _staged_auth(root: str | os.PathLike[str] | None, name: str) -> Any:
    try:
        if root is None:
            raise AdapterError("missing")
        raw = _checked_private_read(Path(root), name, limit=1_000_000)
        value = _strict_json_loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise AdapterError("invalid")
        return value
    except (AdapterError, ValueError, TypeError, UnicodeError):
        raise AuthUnavailable("auth_unavailable") from None


def _auth_token(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1_000_000 or any(ord(c) < 32 or c.isspace() for c in value):
        raise AuthUnavailable("auth_unavailable")
    return value


def _provision_claude(root: str | os.PathLike[str] | None, home: Path, timeout: int) -> Path:
    source = _staged_auth(root, ".credentials.json")
    oauth = source.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise AuthUnavailable("auth_unavailable")
    expires = oauth.get("expiresAt")
    if type(expires) not in {int, float}:
        raise AuthUnavailable("auth_unavailable")
    try:
        finite = math.isfinite(expires)
    except OverflowError:
        finite = False
    if not finite or expires <= (time.time() + timeout + 300 + 60) * 1000:
        raise AuthUnavailable("auth_unavailable")
    allowed = {"accessToken", "expiresAt", "scopes", "subscriptionType", "rateLimitTier"}
    safe = {key: oauth[key] for key in allowed if key in oauth}
    safe["accessToken"] = _auth_token(oauth.get("accessToken"))
    if ("scopes" in safe and (not isinstance(safe["scopes"], list)
                              or any(not isinstance(scope, str) or not scope for scope in safe["scopes"]))):
        raise AuthUnavailable("auth_unavailable")
    for key in ("subscriptionType", "rateLimitTier"):
        if key in safe and safe[key] is not None and not isinstance(safe[key], str):
            raise AuthUnavailable("auth_unavailable")
    directory = home / ".claude"
    directory.mkdir(mode=0o700)
    destination = directory / ".credentials.json"
    _exclusive_private_write(destination, canonical({"claudeAiOauth": safe}).encode())
    return destination


def _auth_capture(evidence):
    """Preserve post-model evidence; suppress all pre-model auth diagnostics."""
    def record(kind, payload):
        if evidence is None:
            return
        if kind in {"cli_result", "cli_timeout"} and isinstance(payload, dict):
            if not _has_model_output(payload.get("stdout", "")):
                payload = {"reason": "auth_unavailable" if _auth_failure(payload) else "pre_model_failure",
                           "returncode": payload.get("returncode"), "timeout": kind == "cli_timeout"}
        evidence(kind, payload)
    return record


def _has_model_output(stdout: str) -> bool:
    for line in _physical_lines(stdout):
        try:
            event = _strict_json_loads(line)
        except AdapterError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in {"assistant", "thinking"}:
            return True
        if isinstance(event.get("item"), dict) and event["item"].get("type") == "agent_message":
            return True
        if event.get("type") == "result" and isinstance(event.get("structured_output"), dict):
            return True
        if isinstance(event.get("step_update"), dict) and event["step_update"].get("step_type") == "agent_response":
            return True
    return False


def _auth_failure(payload: Mapping[str, Any]) -> bool:
    text = (str(payload.get("stdout", "")) + str(payload.get("stderr", ""))).casefold()
    return any(marker in text for marker in ("not logged in", "authentication", "oauth", "unauthorized", "login", "401", "403"))


@contextlib.contextmanager
def _auth_integrity(path: Path, evidence):
    attempt = _ATTEMPT.get()
    relative = path.relative_to(attempt["root"]).as_posix()
    before = _checked_private_read(attempt["root"], relative, limit=1_000_000)
    try:
        yield
    finally:
        try:
            rotated = _checked_private_read(attempt["root"], relative, limit=1_000_000) != before
        except AdapterError:
            rotated = True
        if evidence is not None:
            evidence("native_auth_integrity", {"rotated": rotated, "operator_alert": rotated})
        if rotated:
            raise AuthUnavailable("auth_unavailable") from None


def _controller_evidence(root: Path, catalog: list[dict[str, Any]], evidence) -> tuple[list[dict], int]:
    raw = _checked_private_read(root, "reference-journal.jsonl", limit=16 * 1024 * 1024, allow_empty=True)
    if raw and not raw.endswith(b"\n"):
        raise AdapterError("reference controller journal truncated")
    if evidence is not None:
        evidence("reference_controller", {"raw_base64": base64.b64encode(raw).decode(), "sha256": digest(raw)})
    rows = [_strict_json_loads(line) for line in _physical_lines(raw.decode())]
    calls, metadata = [], 0
    schemas = {tool["name"]: tool["inputSchema"] for tool in catalog}
    from .mcp_proxy import validate_arguments
    for row in rows:
        request, response = row.get("request"), row.get("response")
        if not isinstance(request, dict):
            raise AdapterError("reference controller evidence invalid")
        if request.get("method") != "tools/call":
            method = request.get("method")
            if (method not in {"initialize", "notifications/initialized", "ping", "tools/list", "resources/list"}
                    or (method != "initialize" and request.get("params", {}))):
                raise AdapterError("reference controller forbidden metadata")
            metadata += 1
            continue
        params = request.get("params", {})
        if (not isinstance(params, dict) or not isinstance(response, dict) or "error" in response
                or request.get("jsonrpc") != "2.0" or response.get("jsonrpc") != "2.0"
                or type(request.get("id")) not in {str, int}
                or type(request["id"]) is not type(response.get("id")) or request["id"] != response.get("id")
                or not isinstance(response.get("result"), dict) or response["result"].get("isError")
                or not isinstance(response["result"].get("content"), list)
                or params.get("name") not in schemas or not isinstance(params.get("arguments", {}), dict)):
            raise AdapterError("reference controller content call failed")
        try:
            validate_arguments(params.get("arguments", {}), schemas[params["name"]])
        except ValueError:
            raise AdapterError("reference controller arguments invalid") from None
        calls.append({"name": params["name"], "arguments": params.get("arguments", {}), "result": response["result"],
                      "id": request.get("id")})
    if metadata > METADATA_LIMIT:
        raise AdapterError("tool_limit_error")
    return calls, metadata


def _native_result_equal(output: Any, result: dict) -> bool:
    """Compare a genuine producer value with the controller's complete result."""
    if result.get("isError") or "error" in result or not isinstance(result.get("content"), list):
        return False
    if isinstance(output, str):
        try:
            value = _strict_json_loads(output)
        except AdapterError:
            value = output
    else:
        value = output
    if value == result or value == result["content"]:
        return True
    content = result["content"]
    return (isinstance(value, str) and all(isinstance(block, dict) and block.get("type") == "text"
                                         and isinstance(block.get("text"), str) for block in content)
            and value == "\n".join(block["text"] for block in content))


def _attest_content_calls(native: list[dict], controller: list[dict]) -> None:
    if len(native) != len(controller):
        raise AdapterError("native/controller content count mismatch")
    signatures = [(call["name"], digest(call["arguments"])) for call in controller]
    if len(set(signatures)) != len(signatures):
        raise AdapterError("repeated content call correlation ambiguous")
    for call in native:
        signature = (call.get("name"), digest(call.get("arguments")))
        if signature not in signatures:
            raise AdapterError("native/controller content arguments mismatch")
        index = signatures.index(signature)
        if not _native_result_equal(call.get("output"), controller[index]["result"]):
            raise AdapterError("native/controller content result mismatch")
        signatures[index] = (None, None)


def _claude_content_calls(stdout: str) -> list[dict]:
    pending, completed, seen = {}, [], set()
    for line in _physical_lines(stdout):
        event = _strict_json_loads(line)
        blocks = event.get("message", {}).get("content", [])
        if event.get("type") == "tool_use":
            blocks = [event]
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") != "StructuredOutput":
                ident, name = block.get("id"), block.get("name")
                if not isinstance(ident, str) or ident in seen or not isinstance(name, str) or not name.startswith("mcp__sources__"):
                    raise AdapterError("Claude content call identity invalid")
                seen.add(ident)
                pending[ident] = {"name": name.removeprefix("mcp__sources__"), "arguments": block.get("input")}
            if block.get("type") == "tool_result":
                ident = block.get("tool_use_id")
                if ident not in pending or block.get("is_error") or "content" not in block:
                    raise AdapterError("Claude content result missing, duplicated or failed")
                completed.append({**pending.pop(ident), "output": block["content"]})
    if pending:
        raise AdapterError("Claude content call unfinished")
    return completed


_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_PROJECT_PYTHON = Path(sys.executable)
_SERVER_IDENTITY_TOOL = "mcp_server_identity"
_SERVER_IDENTITY_HASH_KEYS = frozenset({"server_code_sha256", "sources_db_sha256", "vesum_db_sha256"})
_SERVER_IDENTITY_SAFE_KEYS = _SERVER_IDENTITY_HASH_KEYS | frozenset({"sources_db_bytes", "vesum_db_bytes"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOOL_POLICY_ERROR = "tool_policy_error"
_TOOL_LIMIT_ERROR = "tool_limit_error"
_CLAUDE_FLAGS = frozenset(
    {
        "--restricted",
        "--tools",
        "--setting-sources",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--mcp-config",
        "--permission-mode",
        "--no-session-persistence",
        "--output-format",
        "--verbose",
        "--model",
        "--effort",
        "--json-schema",
        "--settings",
    }
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _tool_ref(name: str) -> str:
    return "mcp__sources__" + name


def normalized_reason(exc: BaseException) -> str:
    """Keep failures useful without exporting command output or credentials."""
    if isinstance(exc, AuthUnavailable) or str(exc) == "auth_unavailable":
        return "auth_unavailable"
    if str(exc) in {_TOOL_POLICY_ERROR, _TOOL_LIMIT_ERROR}:
        return str(exc)
    if exc.__class__.__name__ == "RequestBudgetError":
        return "request_budget_error"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_status_{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "network_error"
    text = str(exc).replace("\n", " ").strip().lower()
    for token in (
        "configuration",
        "auth",
        "capability",
        "model",
        "tool",
        "response",
        "timeout",
        "mcp",
        "schema",
        "endpoint",
        "output",
    ):
        if token in text:
            return token + "_error"
    return "adapter_error"


def _require_exact_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise AdapterError(f"{label} contains unsupported fields")


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the public, serialisable evaluator configuration.

    Runtime endpoint and key *values* are read from environment variables.  A
    configuration therefore remains safe to fingerprint or persist.
    """
    if not isinstance(config, Mapping):
        raise AdapterError("configuration must be an object")
    adapter = config.get("adapter")
    if adapter == "opencode":
        from .native_opencode import validate_config as validate_opencode_config

        return validate_opencode_config(config)
    if adapter == "agy":
        from .native_agy import validate_config as validate_agy_config

        return validate_agy_config(config)
    if adapter == "kimi":
        from .native_kimi import validate_config as validate_kimi_config

        return validate_kimi_config(config)
    if adapter == "codex":
        from .native_codex import validate_config as validate_codex_config

        return validate_codex_config(config)
    if adapter == "cursor":
        from .native_cursor import validate_config as validate_cursor_config

        return validate_cursor_config(config)
    base = {
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
    }
    by_adapter = {
        "claude": base | {"claude_bin"},
        "chat-http": base | {"endpoint_env", "key_env", "http_response_format", "openrouter"},
        "responses-http": base | {"endpoint_env", "key_env", "http_response_format"},
    }
    if adapter not in by_adapter:
        raise AdapterError("configuration adapter is unsupported")
    _require_exact_keys(config, by_adapter[adapter], "configuration")
    if config.get("schema") != "zno-nmt.config.v1":
        raise AdapterError("configuration schema mismatch")
    if not isinstance(config.get("model"), str) or not config["model"].strip():
        raise AdapterError("configuration model must be a nonempty string")
    effort = config.get("effort")
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise AdapterError("configuration effort must be string or null")
    for field in ("timeout_seconds", "max_output_tokens", "max_tool_calls", "repeats"):
        value = config.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AdapterError(f"configuration {field} must be positive")
    tools = config.get("tools")
    if not isinstance(tools, list) or any(
        not isinstance(item, str) or not _TOOL_RE.fullmatch(item) or item not in REFERENCE_TOOLS for item in tools
    ):
        raise AdapterError("configuration tools must be allowlisted Sources MCP references")
    if len(tools) != len(set(tools)):
        raise AdapterError("configuration tools contains duplicates")
    corpus_id = config.get("corpus_id")
    if corpus_id is not None and (not isinstance(corpus_id, str) or not corpus_id.strip()):
        raise AdapterError("configuration corpus_id must be string or null")
    provider = config.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise AdapterError("configuration provider must be string or null")
    if adapter == "claude":
        binary = config.get("claude_bin", "claude")
        if not isinstance(binary, str) or not binary.strip():
            raise AdapterError("configuration claude_bin must be a nonempty path")
    else:
        endpoint_env = config.get("endpoint_env")
        if not isinstance(endpoint_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", endpoint_env):
            raise AdapterError("configuration endpoint_env must be an environment variable name")
        key_env = config.get("key_env")
        if key_env is not None and (not isinstance(key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key_env)):
            raise AdapterError("configuration key_env must be an environment variable name or null")
        http_format = config.get("http_response_format", "json_schema")
        http_formats = {"json_schema", "json_object", "text"} if adapter == "chat-http" else {"json_schema", "json_object"}
        if not isinstance(http_format, str) or http_format not in http_formats:
            raise AdapterError("configuration HTTP response format is unsupported")
        if "openrouter" in config:
            routing = config["openrouter"]
            required_routing = {"provider_endpoint", "expected_provider_name", "reasoning_enabled"}
            if not isinstance(routing, Mapping) or not required_routing.issubset(routing) or set(routing) - required_routing - {"max_price"}:
                raise AdapterError("configuration OpenRouter controls are invalid")
            for field in ("provider_endpoint", "expected_provider_name"):
                value = routing[field]
                if not isinstance(value, str) or not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
                    raise AdapterError("configuration OpenRouter provider identity is invalid")
            if type(routing["reasoning_enabled"]) is not bool or (not routing["reasoning_enabled"] and effort is not None):
                raise AdapterError("configuration OpenRouter reasoning controls conflict")
            if "max_price" in routing:
                prices = routing["max_price"]
                if not isinstance(prices, Mapping) or set(prices) != {"prompt", "completion", "request"}:
                    raise AdapterError("configuration OpenRouter price ceilings are incomplete")
                for value in prices.values():
                    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,9})?", value):
                        raise AdapterError("configuration OpenRouter price ceiling must be a nonnegative decimal string")
    if adapter == "responses-http":
        from .responses_http import _runtime_config

        _runtime_config(config)
    return dict(config)


def _condition_policy(config: Mapping[str, Any], condition: str, sources_url: str | None) -> None:
    if condition not in {"closed-book", "sources"}:
        raise AdapterError("condition must be closed-book or sources")
    if condition == "closed-book" and sources_url is not None:
        raise AdapterError("closed-book does not accept a Sources URL")
    if condition == "sources" and (
        not config["tools"] or not isinstance(sources_url, str) or not sources_url.strip()
    ):
        raise AdapterError("sources condition requires nonempty sources URL and tools")


@_isolated_native
def _run_checked(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, cwd=_ATTEMPT.get()["cwd"], env=_ATTEMPT.get()["env"],
                              capture_output=True, text=True, check=False, timeout=_remaining_native_deadline(timeout), umask=0o077)
    except (OSError, subprocess.TimeoutExpired):
        raise AdapterError("CLI capability probe failed") from None



def _claude_auth_contract(binary: str) -> None:
    resolved = shutil.which(binary) or binary
    try:
        actual = hashlib.sha256(Path(resolved).resolve().read_bytes()).hexdigest()
    except OSError:
        raise AdapterError("Claude installed auth contract unavailable") from None
    if actual != CLAUDE_AUTH_CONTRACT:
        raise AdapterError("Claude installed auth contract changed")


@_isolated_native
def _claude_capabilities(config: Mapping[str, Any], *, needs_sources: bool = False) -> tuple[str, str]:
    binary = str(config.get("claude_bin", "claude"))
    _ATTEMPT.get()["env"].update(_child_env(config["max_output_tokens"]))
    _claude_auth_contract(binary)
    help_result = _run_checked([binary, "--help"], timeout=min(15, int(config["timeout_seconds"])))
    if help_result.returncode != 0:
        raise AdapterError("CLI capability probe failed")
    help_text = (help_result.stdout or "") + "\n" + (help_result.stderr or "")
    missing = [flag for flag in _CLAUDE_FLAGS if flag.lower() not in help_text.lower()]
    if missing:
        raise AdapterError("CLI isolation capability unavailable")
    if needs_sources and "--allowedtools" not in help_text.lower():
        raise AdapterError("CLI Sources tool capability unavailable")
    version_result = _run_checked([binary, "--version"], timeout=min(15, int(config["timeout_seconds"])))
    if version_result.returncode != 0:
        raise AdapterError("CLI version unavailable")
    version = (version_result.stdout or "").strip().splitlines()[0] if version_result.stdout else "unknown"
    return binary, version[:160]


@_isolated_native
def _preflight_claude(config: Mapping[str, Any], condition: str) -> str:
    credential_path = _provision_claude(os.environ.get("UKRAINIAN_LLM_EVAL_CLAUDE_PROVISIONING_DIR"),
                                      _ATTEMPT.get()["home"], config["timeout_seconds"])
    with _auth_integrity(credential_path, None):
        _binary, version = _claude_capabilities(config, needs_sources=condition == "sources")
    return version


def preflight(config: Mapping[str, Any], condition: str, sources_url: str | None = None) -> dict[str, Any]:
    """Check only capability and boundaries, returning no endpoint/key values."""
    checked = validate_config(config)
    if checked["adapter"] == "opencode":
        from .native_opencode import preflight as opencode_preflight

        return opencode_preflight(checked, condition, sources_url)
    if checked["adapter"] == "agy":
        from .native_agy import preflight as agy_preflight

        return agy_preflight(checked, condition, sources_url,
                             private_env_path=os.environ.get("UKRAINIAN_LLM_EVAL_AGY_PROVISIONING_DIR"))
    if checked["adapter"] == "kimi":
        from .native_kimi import preflight as kimi_preflight

        return kimi_preflight(
            checked, condition, sources_url,
            private_env_path=os.environ.get("UKRAINIAN_LLM_EVAL_KIMI_PROVISIONING_DIR"),
        )
    if checked["adapter"] == "codex":
        from .native_codex import preflight as codex_preflight

        return codex_preflight(
            checked, condition, sources_url,
            private_env_path=os.environ.get("UKRAINIAN_LLM_EVAL_CODEX_PROVISIONING_DIR"),
        )
    if checked["adapter"] == "cursor":
        from .native_cursor import preflight as cursor_preflight

        return cursor_preflight(checked, condition, sources_url)
    _condition_policy(checked, condition, sources_url)
    capability: dict[str, Any] = {
        "schema": "zno-nmt.capability.v1",
        "adapter": checked["adapter"],
        "condition": condition,
        "requested_model": checked["model"],
        "requested_effort": checked["effort"],
        "tools_sha256": digest(checked["tools"]),
        "corpus_id_sha256": digest(checked["corpus_id"]) if checked["corpus_id"] is not None else None,
        "capability": "unverified",
    }
    if checked["adapter"] == "claude":
        version = _preflight_claude(checked, condition)
        capability.update(capability="native-claude-restricted", cli_version=version)
    else:
        endpoint = os.environ.get(str(checked["endpoint_env"]), "")
        if not endpoint:
            raise AdapterError("HTTP endpoint is unavailable")
        capability.update(capability=checked["adapter"] + "-controller-mediated", cli_version=None)
    if condition == "sources":
        tools, identity = _mcp_list_tools(str(sources_url), int(checked["timeout_seconds"]))
        expected = set(checked["tools"])
        available = {str(item.get("name", "")) for item in tools}
        if not expected.issubset(available):
            raise AdapterError("Sources MCP does not expose configured tools")
        tools = _reference_catalog(tools, checked["tools"])
        capability["reference_catalog"] = tools
        capability["tool_schema_sha256"] = digest(tools)
        capability["mcp_server_identity_sha256"] = identity
    else:
        capability["tool_schema_sha256"] = digest([])
        capability["mcp_server_identity_sha256"] = None
    return capability


def build_prompt(
    packet: Mapping[str, Any], condition: str, *, max_tool_calls: int | None = None,
    reference_catalog: list[dict[str, Any]] | None = None,
    smoke_intent: bool = False,
) -> str:
    """Create the shared, gold-free prompt used by each fresh trial."""
    if not isinstance(smoke_intent, bool):
        raise AdapterError("smoke intent must be a boolean")
    if condition == "closed-book":
        policy = "No tools are available. Answer from your own knowledge."
    elif condition == "sources":
        if isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int) or max_tool_calls <= 0:
            raise AdapterError("tool limit configuration invalid")
        policy = (
            "Only the explicitly provided Sources reference tools may be used. "
            "Do not use a tool to find answers outside that reference corpus. "
            "This Sources condition also tests whether those reference tools work: "
            "you are responsible for exercising them before answering. "
            "Answering from memory without any reference-tool call fails this Sources check. "
            "When an item needs corpus evidence, call those tools and use their structured results "
            "(for example integer match_count) to help decide among the listed options. "
            "A tool count or hit is not itself an option id. "
            "For any наголос/stress item, you MUST call verify_stress on every listed option word "
            "and use the returned stressed forms before selecting an option id. "
            f"You have at most {max_tool_calls} total reference-tool calls for this trial, including failed attempts. "
            "Use them to obtain the needed evidence, and submit answers without further calls before you exceed this limit."
        )
    else:
        raise AdapterError("condition is invalid")
    if packet.get("schema") == GEC_PACKET_SCHEMA:
        task = (
            "You are performing Ukrainian grammatical-error correction. For every opaque item id, return exactly one "
            "corrected Ukrainian sentence while preserving the original meaning. If no correction is needed, return "
            "the original sentence unchanged. Do not annotate edits in the answer field. "
        )
        packet_label = "SENTENCE PACKET:"
    else:
        task = (
            "You are taking a Ukrainian exam. Answer every opaque item id exactly once. "
            "For single-choice items, respond with exactly one listed option id. "
            "For matching items, map every row id to a listed option id. "
            "Never return free-form counts, prose, or values that are not option ids. "
        )
        packet_label = "QUESTION PACKET (contains no answers or scoring key):"
    contract = (
        'Return only JSON: {"responses":{"id":{"answer":<answer or null>,"explanation":<string>}}}. '
        'For each item, give the final answer FIRST in "answer", then a short explanation of why in '
        'the separate "explanation" field, citing Sources if used. The explanation must be nonblank. '
        'Do not provide a chain of thought. Only answers are scored; explanations are unscored review evidence. '
        'Use exactly the packet IDs and exactly these two fields in this order. '
    )
    catalog_text = ""
    if condition == "sources" and smoke_intent:
        if not reference_catalog:
            raise AdapterError("Sources smoke requires a frozen reference catalog")
        policy += (
            " Exercise every tool in the supplied reference catalog with schema-valid arguments before answering. "
            "SMOKE tool checklist: " + ", ".join(tool["name"] for tool in reference_catalog) + "."
        )
    if condition == "sources" and reference_catalog is not None:
        policy += (
            " Use the runtime's reference dispatcher with the listed tool name and arguments; where required, "
            "provide Sources server selection and native tool metadata. No filesystem schema lookup is needed."
        )
        catalog_text = "\n\nTRUSTED REFERENCE CATALOG:\n" + canonical(reference_catalog)
    return (task + contract + policy + catalog_text + "\n\nRESPONSE SCHEMA:\n" + canonical(response_schema(packet))
            + "\n\n" + packet_label + "\n" + canonical(packet))


def _mcq_option_ids(item: Mapping[str, Any]) -> list[str]:
    options = item.get("options", [])
    if not isinstance(options, list):
        return []
    return [str(choice["id"]) for choice in options if isinstance(choice, Mapping) and "id" in choice]


def _mcq_string_schema(option_ids: list[str]) -> dict[str, Any]:
    if option_ids:
        return {"type": "string", "enum": option_ids}
    return {"type": "string"}


def response_schema(packet: Mapping[str, Any]) -> dict[str, Any]:
    items = [item for item in packet.get("items", []) if isinstance(item, Mapping) and "id" in item]
    ids = [str(item["id"]) for item in items]
    if packet.get("schema") == GEC_PACKET_SCHEMA:
        answers = {
            item_id: {
                "anyOf": [
                    {"type": "string", "minLength": 1, "pattern": "^[^\\r\\n\\u0085\\u2028\\u2029]+$"},
                    {"type": "null"},
                ]
            }
            for item_id in ids
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["responses"],
            "properties": {
                "responses": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ids,
                    "properties": _answer_envelope_schemas(answers),
                }
            },
        }
    answers: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item["id"])
        option_ids = _mcq_option_ids(item)
        if item.get("kind") == "matching":
            row_ids = [str(row["id"]) for row in item.get("rows", []) if isinstance(row, Mapping) and "id" in row]
            answers[item_id] = {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": row_ids,
                        "properties": {row_id: _mcq_string_schema(option_ids) for row_id in row_ids},
                    },
                    {"type": "null"},
                ]
            }
        else:
            answers[item_id] = {"anyOf": [_mcq_string_schema(option_ids), {"type": "null"}]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["responses"],
        "properties": {
            "responses": {
                "type": "object",
                "additionalProperties": False,
                "required": ids,
                "properties": _answer_envelope_schemas(answers),
            }
        },
    }


def _answer_envelope_schemas(answers: Mapping[str, Any]) -> dict[str, Any]:
    return {
        item_id: {"type": "object", "additionalProperties": False,
                  "required": ["answer", "explanation"],
                  "properties": {"answer": answer, "explanation": {"type": "string", "pattern": r"\S"}}}
        for item_id, answer in answers.items()
    }


def _reference_catalog(tools: list[dict[str, Any]], configured: list[str]) -> list[dict[str, Any]]:
    """Bind the configured ordered subset of the actual preflight listing."""
    try:
        return normalized_catalog(tools, configured)
    except ValueError as exc:
        raise AdapterError(str(exc)) from None


def prompt_reference_catalog(config: Mapping[str, Any], condition: str, sources_url: str | None) -> list[dict[str, Any]]:
    """Use the same filtered listing for admission prompt-size estimates."""
    if condition == "closed-book" or (condition == "sources" and not config["tools"]):
        return []
    _condition_policy(config, condition, sources_url)
    tools, _identity = _mcp_list_tools(str(sources_url), config["timeout_seconds"])
    return _reference_catalog(tools, config["tools"])


def _child_env(max_output_tokens: int) -> dict[str, str]:
    attempt = _ATTEMPT.get()
    if attempt is None:
        raise AdapterError("native attempt boundary missing")
    env = attempt["env"]
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    # Native refusal fallback must stop instead of sending to a different model.
    # Keep the runtime guard in addition to settings, whose precedence can vary.
    env["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] = "1"
    # Claude's native limit is an environment control.  The version probe
    # cannot attest that a particular CLI build honored it, so receipts retain
    # that distinction as configured rather than observed.
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output_tokens)
    return env


def _parse_sse_or_json(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="strict")
    candidates = [text]
    candidates.extend(line[5:].strip() for line in _physical_lines(text) if line.startswith("data:"))
    for candidate in reversed(candidates):
        try:
            value = _strict_json_loads(candidate)
        except (AdapterError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    raise AdapterError("MCP response is invalid")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError("provider JSON has duplicate keys")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise AdapterError("provider JSON contains non-finite number")


def _strict_json_loads(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant)
    except AdapterError:
        raise
    except json.JSONDecodeError as exc:
        raise AdapterError("provider JSON is invalid") from exc


def _mcp_request(url: str, payload: Mapping[str, Any], timeout: int, session_id: str | None = None) -> tuple[dict[str, Any], str | None]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = urllib.request.Request(url, data=canonical(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=timeout) as response:  # nosec B310 -- operator-configured endpoint
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise AdapterError("MCP response exceeds limit")
            if not raw:
                return {}, response.headers.get("Mcp-Session-Id") or session_id
            return _parse_sse_or_json(raw), response.headers.get("Mcp-Session-Id") or session_id
    except AdapterError:
        raise
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise AdapterError("MCP request failed") from exc


def _safe_mcp_server_identity(response: Mapping[str, Any]) -> str | None:
    """Hash only the server's public identity fields, never endpoint metadata."""
    result = response.get("result")
    if not isinstance(result, Mapping) or result.get("isError") is True:
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None
    text = next(
        (block.get("text") for block in content if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)),
        None,
    )
    if not isinstance(text, str):
        return None
    try:
        value = _strict_json_loads(text)
    except AdapterError:
        return None
    if not isinstance(value, Mapping) or not _SERVER_IDENTITY_HASH_KEYS.issubset(value) or not set(value).issubset(_SERVER_IDENTITY_SAFE_KEYS):
        return None
    identity: dict[str, Any] = {}
    for key in _SERVER_IDENTITY_HASH_KEYS:
        candidate = value.get(key)
        if not isinstance(candidate, str) or not _SHA256_RE.fullmatch(candidate):
            return None
        identity[key] = candidate
    for key in ("sources_db_bytes", "vesum_db_bytes"):
        candidate = value.get(key)
        if candidate is not None:
            if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 0:
                return None
            identity[key] = candidate
    return digest(identity)


def _mcp_list_tools(url: str, timeout: int) -> tuple[list[dict[str, Any]], str | None]:
    session: str | None = None
    initialized, session = _mcp_request(
        url,
        {"jsonrpc": "2.0", "id": "zno-nmt-init", "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "zno-nmt-eval", "version": "1"}}},
        timeout,
    )
    result = initialized.get("result")
    if not isinstance(result, Mapping):
        raise AdapterError("MCP initialize failed")
    _mcp_request(url, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, timeout, session)
    listed, session = _mcp_request(url, {"jsonrpc": "2.0", "id": "zno-nmt-tools", "method": "tools/list", "params": {}}, timeout, session)
    body = listed.get("result")
    tools = body.get("tools") if isinstance(body, Mapping) else None
    if not isinstance(tools, list):
        raise AdapterError("MCP tools/list failed")
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("inputSchema"), Mapping):
            raise AdapterError("MCP tool schema is invalid")
        normalized.append(dict(tool))
    identity = None
    if _SERVER_IDENTITY_TOOL in {tool["name"] for tool in normalized}:
        try:
            identity_response, _session = _mcp_request(
                url,
                {"jsonrpc": "2.0", "id": "zno-nmt-server-identity", "method": "tools/call", "params": {"name": _SERVER_IDENTITY_TOOL, "arguments": {}}},
                timeout,
                session,
            )
            identity = _safe_mcp_server_identity(identity_response)
        except AdapterError:
            # Identity is supplemental capability evidence. A standards-valid
            # server without this tool stays explicitly unknown.
            identity = None
    return normalized, identity


def _mcp_call(url: str, tool_name: str, arguments: Mapping[str, Any], timeout: int) -> Any:
    if not tool_name.startswith("mcp__sources__"):
        raise AdapterError(_TOOL_POLICY_ERROR)
    upstream = tool_name.removeprefix("mcp__sources__")
    if not _TOOL_RE.fullmatch(upstream) or upstream not in REFERENCE_TOOLS:
        raise AdapterError(_TOOL_POLICY_ERROR)
    initialized, session = _mcp_request(url, {"jsonrpc": "2.0", "id": "zno-nmt-init", "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "zno-nmt-eval", "version": "1"}}}, timeout)
    if not isinstance(initialized.get("result"), Mapping):
        raise AdapterError("MCP initialize failed")
    _mcp_request(url, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, timeout, session)
    result, _session = _mcp_request(url, {"jsonrpc": "2.0", "id": "zno-nmt-call", "method": "tools/call", "params": {"name": upstream, "arguments": dict(arguments)}}, timeout, session)
    if "error" in result or not isinstance(result.get("result"), Mapping):
        raise AdapterError("MCP tool call failed")
    return result["result"]


def _extract_responses(value: Any, packet: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"responses"} or not isinstance(value.get("responses"), Mapping):
        raise AdapterError("provider response schema mismatch")
    items = [item for item in packet.get("items", []) if isinstance(item, Mapping) and "id" in item]
    expected = [str(item["id"]) for item in items]
    by_id = {str(item["id"]): item for item in items}
    responses = value["responses"]
    if set(responses) != set(expected):
        raise AdapterError("provider response IDs mismatch")
    normalized: dict[str, Any] = {}
    is_gec = packet.get("schema") == GEC_PACKET_SCHEMA
    for item_id in expected:
        answer = responses[item_id]
        if is_gec:
            if answer is not None and (
                not isinstance(answer, str)
                or not answer.strip()
                or answer.splitlines() != [answer]
            ):
                raise AdapterError("provider GEC response value is invalid")
            normalized[item_id] = answer
            continue
        item = by_id[item_id]
        option_ids = set(_mcq_option_ids(item))
        if answer is None:
            normalized[item_id] = None
            continue
        if item.get("kind") == "matching":
            if not isinstance(answer, Mapping):
                raise AdapterError("provider response value is invalid")
            row_ids = {
                str(row["id"])
                for row in item.get("rows", [])
                if isinstance(row, Mapping) and "id" in row
            }
            if set(answer) != row_ids:
                raise AdapterError("provider response value is invalid")
            normalized_rows: dict[str, str] = {}
            for row_id, option_id in answer.items():
                if not isinstance(row_id, str) or not isinstance(option_id, str):
                    raise AdapterError("provider response value is invalid")
                if option_ids and option_id not in option_ids:
                    raise AdapterError("provider response value is invalid")
                normalized_rows[row_id] = option_id
            normalized[item_id] = normalized_rows
            continue
        if not isinstance(answer, str):
            raise AdapterError("provider response value is invalid")
        if option_ids and answer not in option_ids:
            raise AdapterError("provider response value is invalid")
        normalized[item_id] = answer
    return normalized


def _extract_enveloped_responses(
    value: Any, packet: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str], dict[str, list[str]]]:
    """Parse wire envelopes, leaving the saved flat-map validator untouched.

    Strings are decoded with ordered key pairs and duplicate rejection. Native
    event callers must pass objects directly from that same strict raw decoder,
    before any canonical serialization. Explanations are never normalized.
    """
    if isinstance(value, str):
        value = _strict_json_loads(value)
    if not isinstance(value, Mapping) or set(value) != {"responses"} or not isinstance(value["responses"], Mapping):
        raise AdapterError("provider response schema mismatch")
    answers, explanations, order = {}, {}, {}
    for item_id, envelope in value["responses"].items():
        if not isinstance(envelope, Mapping) or set(envelope) != {"answer", "explanation"}:
            raise AdapterError("provider response envelope fields mismatch")
        explanation = envelope["explanation"]
        if not isinstance(explanation, str) or not explanation.strip():
            raise AdapterError("provider response explanation is blank or invalid")
        order[item_id] = list(envelope)
        if order[item_id] != ["answer", "explanation"]:
            raise AdapterError("provider response field order mismatch")
        answers[item_id] = envelope["answer"]
        explanations[item_id] = explanation
    return _extract_responses({"responses": answers}, packet), explanations, order


def _claude_context_model_mapping(stdout: str) -> dict[str, str]:
    """Resolve a context selector only when native terminal metadata attests it."""
    events = [_strict_json_loads(line) for line in _physical_lines(stdout)]
    initial = {event.get("model") for event in events if isinstance(event, Mapping)
               and event.get("type") == "system" and event.get("subtype") == "init"
               and isinstance(event.get("model"), str)}
    decorated = {model for model in initial if model.endswith("[1m]")}
    if not decorated:
        return {}
    if len(initial) != 1:
        raise AdapterError("CLI model drift")
    selector = next(iter(decorated))
    terminal = [event for event in events if isinstance(event, Mapping) and event.get("type") == "result"]
    if len(terminal) != 1 or terminal[0].get("is_error") is True:
        raise AdapterError("CLI model drift")
    usage = terminal[0].get("modelUsage")
    if not isinstance(usage, Mapping) or set(usage) != {selector}:
        raise AdapterError("CLI model drift")
    record = usage[selector]
    if (not isinstance(record, Mapping) or record.get("canonicalModel") != selector[:-4]
            or record.get("contextWindow") != 1_000_000):
        raise AdapterError("CLI model drift")
    return {selector: record["canonicalModel"]}


def _parse_stream_json(
    stdout: str, packet: Mapping[str, Any], tools: set[str], max_tools: int
) -> tuple[dict[str, Any], str, int, dict[str, int | float | None]]:
    result_text: str | None = None
    structured_output: Mapping[str, Any] | None = None
    submissions: list[Any] = []
    observed_models: set[str] = set()
    observed_tools: list[str] = []
    init_seen = False
    usage: dict[str, int | float | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None, "cost_usd": None}
    for line in _physical_lines(stdout):
        try:
            event = _strict_json_loads(line)
        except AdapterError as exc:
            raise AdapterError("CLI emitted invalid stream JSON") from exc
        if not isinstance(event, Mapping):
            raise AdapterError("CLI emitted invalid stream event")
        structured = event.get("structured_output")
        if isinstance(structured, Mapping):
            if event.get("type") != "result" or structured_output is not None:
                raise AdapterError("CLI structured result evidence is duplicated or misplaced")
            structured_output = structured
        model = event.get("model")
        if model is None and isinstance(event.get("message"), Mapping):
            model = event["message"].get("model")
        if isinstance(model, str) and model:
            observed_models.add(model)
        if event.get("type") == "system" and event.get("subtype") == "init":
            init_seen = True
            raw_tools = event.get("tools", [])
            if not isinstance(raw_tools, list):
                raise AdapterError("CLI init tool surface is malformed")
            announced = {item if isinstance(item, str) else item.get("name") for item in raw_tools if isinstance(item, (str, Mapping))}
            if None in announced or not announced.issubset(tools | {"StructuredOutput"}):
                raise AdapterError(_TOOL_POLICY_ERROR)
            if not tools and announced - {"StructuredOutput"}:
                raise AdapterError(_TOOL_POLICY_ERROR)
            if tools and not tools.issubset(announced):
                raise AdapterError(_TOOL_POLICY_ERROR)
        content = event.get("message", {}).get("content", []) if isinstance(event.get("message"), Mapping) else []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "tool_use":
                    name = block.get("name")
                    if not isinstance(name, str):
                        raise AdapterError("CLI emitted malformed tool call")
                    if name == "StructuredOutput":
                        submissions.append(block.get("input"))
                        continue
                    observed_tools.append(name)
        if event.get("type") == "tool_use":
            name = event.get("name")
            if not isinstance(name, str):
                raise AdapterError("CLI emitted malformed tool call")
            if name == "StructuredOutput":
                submissions.append(event.get("input"))
                continue
            observed_tools.append(name)
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            if event.get("is_error") is True:
                raise AdapterError("CLI result is error")
            result_text = event["result"]
            event_usage = event.get("usage")
            if isinstance(event_usage, Mapping):
                usage["input_tokens"] = _nonnegative_int(event_usage.get("input_tokens", event_usage.get("prompt_tokens")))
                usage["output_tokens"] = _nonnegative_int(event_usage.get("output_tokens", event_usage.get("completion_tokens")))
                usage["total_tokens"] = _nonnegative_int(event_usage.get("total_tokens"))
            usage["cost_usd"] = _nonnegative_number(event.get("total_cost_usd", event.get("cost_usd")))
        elif event.get("type") == "result" and event.get("is_error") is True:
            raise AdapterError("CLI result is error")
    if any(name not in tools for name in observed_tools):
        raise AdapterError(_TOOL_POLICY_ERROR)
    if len(observed_tools) > max_tools:
        raise AdapterError(_TOOL_LIMIT_ERROR)
    if not init_seen:
        raise AdapterError("CLI init evidence missing")
    model_mapping = _claude_context_model_mapping(stdout)
    observed_models = {model_mapping.get(model, model) for model in observed_models}
    if len(observed_models) > 1:
        raise AdapterError("CLI model drift")
    if result_text is None and structured_output is None:
        raise AdapterError("CLI response missing result")
    if structured_output is not None:
        payload: Any = dict(structured_output)
    else:
        try:
            payload = _strict_json_loads(result_text or "")
        except AdapterError as exc:
            if packet.get("schema") == GEC_PACKET_SCHEMA:
                raise
            raise AdapterError("CLI response is not JSON") from exc
    # Both objects still carry raw stream key order from _strict_json_loads.
    if structured_output is None or len(submissions) != 1:
        raise AdapterError("CLI StructuredOutput input evidence missing or duplicated")
    parsed = _extract_enveloped_responses(payload, packet)
    if _extract_enveloped_responses(submissions[0], packet) != parsed:
        raise AdapterError("CLI StructuredOutput and result evidence disagree")
    return parsed[0], next(iter(observed_models), "unknown"), len(observed_tools), usage


def _claude_session_identity(stdout: str) -> str:
    """Use native session evidence so a repeated session cannot look fresh."""
    sessions: set[str] = set()
    terminal_session = None
    for line in _physical_lines(stdout):
        event = _strict_json_loads(line)
        if not isinstance(event, Mapping):
            raise AdapterError("CLI session evidence is malformed")
        session = event.get("session_id")
        if session is not None:
            if not isinstance(session, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session) is None:
                raise AdapterError("CLI session identity is malformed")
            sessions.add(session)
        if event.get("type") == "result":
            terminal_session = session
    if len(sessions) != 1 or terminal_session not in sessions:
        raise AdapterError("CLI session identity is missing or inconsistent")
    return terminal_session


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative_number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _run_claude_process(argv: list[str], *, cwd: Path, env: Mapping[str, str], prompt: str, timeout: int, evidence: Callable[[str, Any], None] | None = None) -> subprocess.CompletedProcess[str]:
    """Kill the whole CLI process group so an MCP proxy cannot survive a timeout."""
    evidence = _auth_capture(evidence)
    try:
        if evidence is not None:
            evidence("candidate_submission", {"stdin": prompt, "sha256": hashlib.sha256(prompt.encode()).hexdigest()})
        process = subprocess.Popen(argv, cwd=str(cwd), env=dict(env), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, umask=0o077)
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (NameError, ProcessLookupError, OSError):
            if "process" in locals():
                process.kill()
        if "process" in locals():
            stdout, stderr = process.communicate()
            if evidence is not None:
                evidence("cli_timeout", {"stdout": stdout, "stderr": stderr, "returncode": process.returncode})
        if "stdout" in locals() and not _has_model_output(stdout) and _auth_failure({"stdout": stdout, "stderr": stderr}):
            raise AuthUnavailable("auth_unavailable") from None
        raise AdapterError("CLI timeout") from None
    except OSError as exc:
        raise AdapterError("CLI invocation failed") from exc
    return subprocess.CompletedProcess(argv, process.returncode, stdout=stdout, stderr=stderr)


@_isolated_native
def run_claude(packet: Mapping[str, Any], config: Mapping[str, Any], condition: str, *, sources_url: str | None, prompt: str, evidence: Callable[[str, Any], None] | None = None, reference_catalog: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run one fresh restricted Claude CLI session and return sanitized evidence."""
    checked = validate_config(config)
    if checked["adapter"] != "claude":
        raise AdapterError("wrong adapter")
    _condition_policy(checked, condition, sources_url)
    attempt = _ATTEMPT.get()
    credential_path = _provision_claude(os.environ.get("UKRAINIAN_LLM_EVAL_CLAUDE_PROVISIONING_DIR"),
                                        attempt["home"], checked["timeout_seconds"])
    evidence = _auth_capture(evidence)
    with _auth_integrity(credential_path, evidence):
        binary, cli_version = _claude_capabilities(checked, needs_sources=condition == "sources")
    schema = response_schema(packet)
    with contextlib.nullcontext(attempt["root"]) as root:
        mcp_config = root / "mcp.json"
        if condition == "closed-book":
            mcp = {"mcpServers": {}}
        else:
            proxy = Path(__file__).with_name("mcp_proxy.py").resolve()
            if not proxy.is_file():
                raise AdapterError("Sources MCP proxy unavailable")
            if not _PROJECT_PYTHON.is_file():
                raise AdapterError("project interpreter unavailable")
            mcp = {"mcpServers": {"sources": {"command": str(_PROJECT_PYTHON), "args": [str(proxy), "--url-env", "ZNO_NMT_SOURCES_URL", "--tools", canonical(checked["tools"]), "--max-tool-calls", str(checked["max_tool_calls"]), "--journal", str(root / "reference-journal.jsonl"),
                    "--deadline", str(attempt["deadline"])], "env": {"ZNO_NMT_SOURCES_URL": str(sources_url)}}}}
        mcp_config.write_text(canonical(mcp) + "\n", encoding="utf-8")
        mcp_config.chmod(0o600)
        argv = [
            binary,
            "-p",
            "--restricted",
            "--settings",
            canonical({"claudeMdExcludes": ["**"], "disableAllHooks": True,
                       "switchModelsOnFlag": False, "availableModels": [checked["model"]]}),
            "--setting-sources",
            "",
            "--tools",
            "",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_config),
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            checked["model"],
            "--json-schema",
            canonical(schema),
        ]
        if checked["effort"] is not None:
            argv.extend(["--effort", checked["effort"]])
        if condition == "sources":
            argv.extend(["--allowedTools", ",".join(_tool_ref(tool) for tool in checked["tools"])])
        started = time.monotonic()
        process_options = {"evidence": evidence} if evidence is not None else {}
        with _auth_integrity(credential_path, evidence):
            completed = _run_claude_process(argv, cwd=attempt["cwd"], env=_child_env(checked["max_output_tokens"]),
                prompt=prompt, timeout=_remaining_native_deadline(checked["timeout_seconds"]), **process_options)
        elapsed = time.monotonic() - started
        _claude_auth_contract(binary)
        if evidence is not None:
            evidence("cli_result", {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr})
        if not _has_model_output(completed.stdout) and _auth_failure({"stdout": completed.stdout, "stderr": completed.stderr}):
            raise AuthUnavailable("auth_unavailable")
        if completed.returncode != 0:
            failure_text = (completed.stdout or "") + "\n" + (completed.stderr or "")
            if any(marker in failure_text.casefold() for marker in ("not logged in", "login", "authentication", "oauth", "keychain")):
                raise AuthUnavailable("auth_unavailable")
            raise AdapterError("CLI invocation failed")
        observed_tools = {_tool_ref(tool) for tool in checked["tools"]} if condition == "sources" else set()
        responses, effective_model, tool_calls, usage = _parse_stream_json(completed.stdout, packet, observed_tools, checked["max_tool_calls"])
        metadata_count = 0
        if condition == "sources":
            calls, metadata_count = _controller_evidence(root, reference_catalog or [], evidence)
            _attest_content_calls(_claude_content_calls(completed.stdout), calls)
    model_mapping = _claude_context_model_mapping(completed.stdout)
    expected_model = model_mapping.get(checked["model"], checked["model"])
    if effective_model != "unknown" and effective_model != expected_model:
        raise AdapterError("CLI model drift")
    session_id = _claude_session_identity(completed.stdout)
    return {
        "responses": responses,
        "identity": {"model_context_mapping": model_mapping, "adapter": "claude", "harness": "claude-cli", "model": checked["model"], "provider": checked.get("provider") or "claude-cli", "session_id": session_id, "requested_model": checked["model"], "effective_model": effective_model, "requested_effort": checked["effort"], "effective_effort": "unknown", "cli_version": cli_version, "tool_schema_sha256": digest(checked["tools"] if condition == "sources" else []), "corpus_id_sha256": digest(checked["corpus_id"]) if checked["corpus_id"] is not None else None, "max_output_tokens_configured": checked["max_output_tokens"], "max_output_tokens_effective": "unknown"},
        "metrics": {"elapsed_seconds": elapsed, "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"], "total_tokens": usage["total_tokens"], "cost_usd": usage["cost_usd"], "tool_calls": tool_calls,
                    "controller_metadata_operations": metadata_count, "native_metadata_operations": "unknown"},
    }


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _http_json(url: str, payload: Mapping[str, Any] | bytes, *, key: str | None, timeout: float, max_bytes: int, evidence: Callable[[str, Any], None] | None = None) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AdapterError("HTTP endpoint scheme invalid")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request_bytes = payload if isinstance(payload, bytes) else canonical(payload).encode("utf-8")
    request = urllib.request.Request(url, data=request_bytes, headers=headers, method="POST")
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=timeout) as response:  # nosec B310 -- operator-configured endpoint
            raw = response.read(max_bytes + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise AdapterError("HTTP completion failed") from exc
    if evidence is not None:
        evidence("completion_body", {"base64": base64.b64encode(raw).decode("ascii"), "truncated": len(raw) > max_bytes})
    if len(raw) > max_bytes:
        raise AdapterError("HTTP completion exceeds limit")
    try:
        value = _strict_json_loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, AdapterError) as exc:
        raise AdapterError("HTTP completion response invalid") from exc
    if not isinstance(value, dict):
        raise AdapterError("HTTP completion response invalid")
    return value


def run_chat_http(packet: Mapping[str, Any], config: Mapping[str, Any], condition: str, *, sources_url: str | None, prompt: str, evidence: Callable[[str, Any], None] | None = None, request_budget: Any = None) -> dict[str, Any]:
    """Run one controller-mediated chat-completions session.

    Tool calls are checked before the MCP broker receives them; model text can
    never become a shell command or an unrestricted MCP request.
    """
    checked = validate_config(config)
    if checked["adapter"] != "chat-http":
        raise AdapterError("wrong adapter")
    _condition_policy(checked, condition, sources_url)
    endpoint = os.environ.get(str(checked["endpoint_env"]), "")
    key = os.environ.get(str(checked["key_env"]), "") if checked.get("key_env") else None
    if not endpoint:
        raise AdapterError("HTTP runtime endpoint unavailable")
    started = time.monotonic()
    deadline = started + checked["timeout_seconds"]
    mcp_tools: list[dict[str, Any]] = []
    identity: str | None = None
    if condition == "sources":
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterError("HTTP total timeout")
        mcp_tools, identity = _mcp_list_tools(str(sources_url), max(1, int(remaining)))
    available = {tool["name"]: tool for tool in mcp_tools}
    if condition == "sources" and not set(checked["tools"]).issubset(available):
        raise AdapterError("Sources MCP does not expose configured tools")
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    max_bytes = min(2_000_000, max(65_536, checked["max_output_tokens"] * 64))
    response_name = "ua_gec_responses" if packet.get("schema") == GEC_PACKET_SCHEMA else "zno_nmt_responses"
    output_parameter = request_budget.output_parameter_name if request_budget is not None else "max_tokens"
    request_base: dict[str, Any] = {"model": checked["model"], "messages": messages, output_parameter: checked["max_output_tokens"], "response_format": {"type": "json_schema", "json_schema": {"name": response_name, "strict": True, "schema": response_schema(packet)}}}
    if checked.get("http_response_format") == "json_object":
        request_base["response_format"] = {"type": "json_object"}
    elif checked.get("http_response_format") == "text":
        del request_base["response_format"]
    routing = checked.get("openrouter")
    if routing is not None:
        request_base["provider"] = {"only": [routing["provider_endpoint"]], "allow_fallbacks": False, "require_parameters": True}
        if "max_price" in routing:
            request_base["provider"]["max_price"] = dict(routing["max_price"])
        request_base["reasoning"] = {"enabled": routing["reasoning_enabled"]}
        if checked["effort"] is not None:
            request_base["reasoning"]["effort"] = checked["effort"]
    elif checked["effort"] is not None:
        request_base["reasoning_effort"] = checked["effort"]
    if condition == "sources":
        request_base["tools"] = [{"type": "function", "function": {"name": _tool_ref(name), "description": "Approved Sources reference tool", "parameters": tool["inputSchema"]}} for name, tool in available.items() if name in checked["tools"]]
        request_base["tool_choice"] = "auto"
    tool_calls = 0
    retrieval_digests: list[str] = []
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    usage_known = {key: False for key in usage_totals}
    reported_cost = Decimal(0)
    reported_cost_known = False
    response: dict[str, Any] | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterError("HTTP total timeout")
        if evidence is not None:
            evidence("completion_request", request_base)
        prepared = None
        request_payload: Mapping[str, Any] | bytes = request_base
        if request_budget is not None:
            request_payload, prepared = request_budget.commit_request(request_base)
            if evidence is not None:
                evidence("request_budget_commitment", prepared)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterError("HTTP total timeout")
        http_options = {"evidence": evidence} if evidence is not None else {}
        body = _http_json(endpoint, request_payload, key=key, timeout=remaining, max_bytes=max_bytes, **http_options)
        if evidence is not None:
            evidence("completion_response", body)
        if body.get("model") != checked["model"]:
            raise AdapterError("HTTP model drift")
        if routing is not None and body.get("provider") != routing["expected_provider_name"]:
            raise AdapterError("HTTP provider identity drift")
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise AdapterError("HTTP completion choices invalid")
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise AdapterError("HTTP completion message invalid")
        calls = message.get("tool_calls")
        finish_reason = choices[0].get("finish_reason")
        if finish_reason == "length":
            raise AdapterError("HTTP completion output limit")
        body_usage = body.get("usage")
        if isinstance(body_usage, Mapping):
            for usage_key, provider_key in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("total_tokens", "total_tokens")):
                value = _nonnegative_int(body_usage.get(provider_key))
                if value is not None:
                    usage_totals[usage_key] += value
                    usage_known[usage_key] = True
            raw_cost = body_usage.get("cost")
            if raw_cost is not None:
                if isinstance(raw_cost, bool) or not isinstance(raw_cost, (int, float, str)):
                    raise AdapterError("HTTP completion cost invalid")
                try:
                    round_cost = Decimal(str(raw_cost))
                except InvalidOperation as exc:
                    raise AdapterError("HTTP completion cost invalid") from exc
                if not round_cost.is_finite() or round_cost < 0:
                    raise AdapterError("HTTP completion cost invalid")
                reported_cost += round_cost
                reported_cost_known = True
        round_tool_calls = len(calls) if isinstance(calls, list) else 0
        if request_budget is not None:
            observation = request_budget.observe(body_usage, tool_calls=round_tool_calls)
            if evidence is not None:
                evidence("request_budget_observation", observation)
        if not calls:
            response = body
            content = message.get("content")
            if not isinstance(content, str):
                raise AdapterError("HTTP completion content invalid")
            try:
                responses = _extract_enveloped_responses(content, packet)[0]
            except AdapterError as exc:
                if packet.get("schema") == GEC_PACKET_SCHEMA:
                    raise
                raise AdapterError("HTTP completion is not JSON") from exc
            break
        if condition != "sources" or not isinstance(calls, list):
            raise AdapterError(_TOOL_POLICY_ERROR)
        messages.append(dict(message))
        for call in calls:
            tool_calls += 1
            if tool_calls > checked["max_tool_calls"]:
                raise AdapterError(_TOOL_LIMIT_ERROR)
            if not isinstance(call, Mapping):
                raise AdapterError(_TOOL_POLICY_ERROR)
            function = call.get("function")
            name = function.get("name") if isinstance(function, Mapping) else None
            raw_args = function.get("arguments") if isinstance(function, Mapping) else None
            if not isinstance(name, str) or name not in {_tool_ref(tool) for tool in checked["tools"]} or not isinstance(raw_args, str):
                raise AdapterError(_TOOL_POLICY_ERROR)
            try:
                arguments = _strict_json_loads(raw_args)
            except AdapterError as exc:
                raise AdapterError("HTTP tool arguments invalid") from exc
            if not isinstance(arguments, Mapping):
                raise AdapterError("HTTP tool arguments invalid")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError("HTTP total timeout")
            if evidence is not None:
                evidence("tool_request", {"name": name, "arguments": arguments, "id": call.get("id")})
            broker_result = _mcp_call(str(sources_url), name, arguments, max(1, int(remaining)))
            serialized_result = (
                request_budget.serialize_tool_result(broker_result)
                if request_budget is not None
                else canonical(broker_result)
            )
            if evidence is not None:
                evidence("tool_result", {"id": call.get("id"), "result": broker_result})
            retrieval_digests.append(digest(broker_result))
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise AdapterError("HTTP tool call id invalid")
            messages.append({"role": "tool", "tool_call_id": call_id, "content": serialized_result})
    assert response is not None
    trial = {
        "responses": responses,
        "identity": {"adapter": "chat-http", "harness": "chat-http", "model": checked["model"], "provider": checked.get("provider") or "chat-http", "session_id": str(uuid.uuid4()), "requested_model": checked["model"], "effective_model": checked["model"], "requested_effort": checked["effort"], "effective_effort": "unknown", "cli_version": None, "tool_schema_sha256": digest(mcp_tools), "mcp_server_identity_sha256": identity, "corpus_id_sha256": digest(checked["corpus_id"]) if checked["corpus_id"] is not None else None, "retrieval_receipt_sha256": digest(retrieval_digests) if retrieval_digests else None},
        "metrics": {"elapsed_seconds": time.monotonic() - started, "input_tokens": usage_totals["input_tokens"] if usage_known["input_tokens"] else None, "output_tokens": usage_totals["output_tokens"] if usage_known["output_tokens"] else None, "total_tokens": usage_totals["total_tokens"] if usage_known["total_tokens"] else None, "cost_usd": float(reported_cost) if reported_cost_known else None, "tool_calls": tool_calls},
    }
    if routing is not None:
        trial["identity"].update(provider_endpoint_requested=routing["provider_endpoint"],
                                 effective_provider=response["provider"],
                                 reasoning_enabled_requested=routing["reasoning_enabled"])
    return trial
