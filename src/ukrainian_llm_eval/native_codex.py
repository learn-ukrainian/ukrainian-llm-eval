"""Fail-closed adapter for a provisioned native Codex CLI subscription.

The adapter does not discover accounts, invoke login, or use HTTP credentials.
Its runtime-only ``private_env_path`` is a small owner-only provisioning
directory containing exactly ``auth.json`` plus a locally captured control
receipt.  Each attempt copies only that authentication file into a fresh
``CODEX_HOME``; it never points Codex at the caller's normal home.

Only the closed-book condition is implemented.  Sources is deliberately
unsupported until a separate adapter has empirical isolation evidence for the
MCP surface.  A control receipt is mandatory because feature flags describe a
request, while a mock capture is needed to establish what visible handlers
actually do.  Visible inert descriptors are acceptable; unknown or usable
handlers are not.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any

from . import adapters
from .candidate_outcome import CANDIDATE_RESPONSE_ERROR

CODEX_ADAPTER = "codex"
CODEX_PROVIDER = "managed:codex-subscription"
CODEX_SCHEMA = "zno-nmt.config.v1"
CODEX_CAPABILITY_SCHEMA = "zno-nmt.capability.v1"
CODEX_HARNESS = "codex-cli"
CODEX_AUTH_FILE = "auth.json"
CODEX_CONTROL_FILE = "closed-book-control.json"
_MODEL_RE = re.compile(r"^gpt-[A-Za-z0-9._-]{1,120}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
_MAX_PRIVATE_FILE_BYTES = 1_000_000
_MAX_CONTROL_ARTIFACT_BYTES = 20_000_000
_MAX_OUTPUT_BYTES = 2_000_000
_PROCESS_CLEANUP_SECONDS = 1
_MAX_HELP_BYTES = 256_000
_MAX_VERSION_BYTES = 16_384
_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "multi_agent", "multi_agent_v2", "apps", "plugins",
    "remote_plugin", "browser_use", "browser_use_external", "computer_use", "view_image",
    "skill_search", "workspace_dependencies", "memories", "tool_suggest", "sleep_tool",
    "code_mode", "code_mode_host",
)
_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "schema", "adapter", "model", "effort", "timeout_seconds", "max_output_tokens",
        "max_tool_calls", "repeats", "tools", "corpus_id", "provider", "codex_bin",
    }
)
_SAFE_CHILD_ENV = frozenset({"PATH", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"})
_REQUIRED_HELP_FLAGS = frozenset(
    {
        "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
        "--strict-config", "--json", "--output-schema", "--model", "--disable", "--enable",
    }
)
_INERT_HANDLER_NAMES = frozenset(
    {
        "functions.exec", "functions.wait", "functions.request_user_input", "delegation",
    }
)
_AUTH_KEYS = frozenset({"OPENAI_API_KEY", "auth_mode", "last_refresh", "tokens"})
_AUTH_TOKEN_KEYS = frozenset({"access_token", "account_id", "id_token", "refresh_token"})
_CONTROL_EVIDENCE_KEYS = frozenset({"source_kind", "artifact_path", "artifact_sha256", "request_shape_sha256"})


class CodexAdapterError(adapters.AdapterError):
    """A native Codex route did not satisfy the evaluator boundary."""


@dataclass(frozen=True)
class NativeCodexOptions:
    config: dict[str, Any]
    private_env_path: Path
    binary: str


@dataclass(frozen=True)
class _CliProbe:
    binary: str
    binary_path: Path
    entrypoint_sha256: str
    native_runtime_path: Path
    native_runtime_sha256: str
    version: str


@dataclass(frozen=True)
class _ParsedCodexEvents:
    """Envelope-verified Codex output and any answer-only failure."""

    responses: dict[str, Any] | None
    session_id: str
    usage: dict[str, int | None]
    answer_failure_reason: str | None
    answer_content: str


def _fail(message: str) -> CodexAdapterError:
    return CodexAdapterError(message)


def _text(value: Any, label: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > max_length:
        raise _fail(f"{label} must be a nonempty string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _fail(f"{label} contains control characters")
    return value


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _fail(f"{label} must be positive")
    return value


def _exact_keys(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    if set(value) - set(allowed):
        raise _fail(f"{label} contains unsupported fields")


def validate_codex_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the native-only fields without reading private provisioning."""

    if not isinstance(config, Mapping):
        raise _fail("configuration must be an object")
    adapter = config.get("adapter")
    if adapter is not None and adapter != CODEX_ADAPTER:
        raise _fail("configuration adapter is not codex")
    model = _text(config.get("model"), "configuration model")
    if _MODEL_RE.fullmatch(model) is None:
        raise _fail("configuration model must be an exact gpt model alias")
    return {**dict(config), "model": model, "codex_bin": _text(config.get("codex_bin", "codex"), "configuration codex_bin", max_length=1024)}


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the serialisable Codex route, never credential values."""

    checked = validate_codex_config(config)
    _exact_keys(checked, _ALLOWED_CONFIG_KEYS, "configuration")
    if checked.get("schema") != CODEX_SCHEMA or checked.get("adapter") != CODEX_ADAPTER:
        raise _fail("configuration schema or adapter mismatch")
    effort = _text(checked.get("effort"), "configuration effort", max_length=32)
    if effort not in _EFFORTS:
        raise _fail("configuration effort is unsupported")
    for field in ("timeout_seconds", "max_output_tokens", "max_tool_calls", "repeats"):
        _positive(checked.get(field), f"configuration {field}")
    if checked.get("tools") != []:
        raise _fail("native Codex supports closed-book only; tools must be empty")
    corpus_id = checked.get("corpus_id")
    if corpus_id is not None:
        corpus_id = _text(corpus_id, "configuration corpus_id")
    provider = checked.get("provider")
    if provider is not None and provider != CODEX_PROVIDER:
        raise _fail("configuration provider identity is contradictory")
    return {
        **checked,
        "effort": effort,
        "tools": [],
        "corpus_id": corpus_id,
        "provider": CODEX_PROVIDER,
    }


def _owner_directory(value: str | os.PathLike[str] | Path | None) -> Path:
    if value is None:
        raise _fail("native Codex provisioning is unresolved; private_env_path is required")
    raw = os.fspath(value) if isinstance(value, (str, os.PathLike, Path)) else None
    if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
        raise _fail("private_env_path must be an absolute directory")
    path = Path(raw)
    try:
        if path.is_symlink() or not path.is_dir():
            raise _fail("private_env_path must be a directory")
        path = path.resolve(strict=True)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise _fail("private_env_path must be owner-only")
    except OSError as exc:
        raise _fail("private_env_path is unavailable") from exc
    try:
        if {entry.name for entry in path.iterdir()} != {CODEX_AUTH_FILE, CODEX_CONTROL_FILE}:
            raise _fail("private_env_path must contain only sanitized Codex provisioning")
    except OSError as exc:
        raise _fail("private_env_path is unavailable") from exc
    return path


def _private_file(root: Path, name: str, label: str) -> Path:
    candidate = root / name
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        mode = stat.S_IMODE(resolved.stat().st_mode)
    except (OSError, ValueError) as exc:
        raise _fail(f"{label} is unavailable") from exc
    if candidate.is_symlink() or not resolved.is_file() or mode & 0o077:
        raise _fail(f"{label} must be an owner-only regular file")
    if resolved.stat().st_size > _MAX_PRIVATE_FILE_BYTES:
        raise _fail(f"{label} exceeds limit")
    return resolved


def _secret_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_PRIVATE_FILE_BYTES:
        raise _fail(f"{label} is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _fail(f"{label} is invalid")
    return value


def _sanitized_chatgpt_auth(root: Path) -> bytes:
    """Return the current Codex ChatGPT auth subset without exposing secrets."""

    path = _private_file(root, CODEX_AUTH_FILE, "private Codex auth")
    try:
        parsed = adapters._strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, adapters.AdapterError) as exc:
        raise _fail("private Codex auth is invalid") from exc
    if not isinstance(parsed, Mapping):
        raise _fail("private Codex auth is invalid")
    _exact_keys(parsed, _AUTH_KEYS, "private Codex auth")
    if parsed.get("auth_mode") != "chatgpt":
        raise _fail("private Codex auth is not ChatGPT subscription auth")
    if parsed.get("OPENAI_API_KEY") not in (None, ""):
        raise _fail("private Codex API-key auth is unsupported")
    _secret_text(parsed.get("last_refresh"), "private Codex last_refresh")
    tokens = parsed.get("tokens")
    if not isinstance(tokens, Mapping):
        raise _fail("private Codex tokens are invalid")
    if set(tokens) != _AUTH_TOKEN_KEYS:
        raise _fail("private Codex tokens are incomplete")
    _exact_keys(tokens, _AUTH_TOKEN_KEYS, "private Codex tokens")
    safe_tokens = {field: _secret_text(tokens.get(field), f"private Codex token {field}") for field in sorted(_AUTH_TOKEN_KEYS)}
    safe = {"OPENAI_API_KEY": None, "auth_mode": "chatgpt", "last_refresh": parsed["last_refresh"], "tokens": safe_tokens}
    return json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_options(config: Mapping[str, Any], *, private_env_path: str | os.PathLike[str] | Path | None) -> NativeCodexOptions:
    checked = validate_config(config)
    root = _owner_directory(private_env_path)
    _sanitized_chatgpt_auth(root)
    _private_file(root, CODEX_CONTROL_FILE, "native Codex control receipt")
    return NativeCodexOptions(config=checked, private_env_path=root, binary=checked["codex_bin"])


def _run_checked(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail("Codex CLI capability probe failed") from exc


def _native_runtime_target() -> tuple[str, str] | None:
    """Return the package and vendor target selected by the supported JS launcher."""

    package_by_platform = {
        "Darwin": {
            "arm64": ("codex-darwin-arm64", "aarch64-apple-darwin"),
            "x86_64": ("codex-darwin-x64", "x86_64-apple-darwin"),
        },
        "Linux": {
            "aarch64": ("codex-linux-arm64", "aarch64-unknown-linux-musl"),
            "x86_64": ("codex-linux-x64", "x86_64-unknown-linux-musl"),
        },
        "Windows": {
            "ARM64": ("codex-win32-arm64", "aarch64-pc-windows-msvc"),
            "AMD64": ("codex-win32-x64", "x86_64-pc-windows-msvc"),
        },
    }
    return package_by_platform.get(platform.system(), {}).get(platform.machine())


def _is_direct_native_executable(path: Path) -> bool:
    """Recognize only ordinary native executable formats, never an arbitrary wrapper."""

    try:
        mode = path.stat().st_mode
        header = path.read_bytes()[:4]
    except OSError:
        return False
    if not mode & 0o111:
        return False
    return header in {
        b"\x7fELF",  # ELF
        b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit
        b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",  # Mach-O 32-bit
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",  # Mach-O universal 32-bit
        b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",  # Mach-O universal 64-bit
        b"MZ\x90\x00",  # PE (the common DOS stub prefix)
    }


def _native_runtime_for(entrypoint: Path) -> Path:
    """Resolve a direct native binary or the installed Codex JS launcher's payload.

    A wrapper without the known package layout is deliberately unsupported:
    hashing it alone would not bind the executable that actually runs.
    """

    if _is_direct_native_executable(entrypoint):
        return entrypoint
    try:
        launcher_prefix = entrypoint.read_bytes()[:256]
    except OSError as exc:
        raise _fail("Codex CLI binary is unreadable") from exc
    if not launcher_prefix.startswith(b"#!/usr/bin/env node"):
        raise _fail("Codex CLI runtime closure is unsupported")
    target = _native_runtime_target()
    if target is None or entrypoint.name != "codex.js" or entrypoint.parent.name != "bin":
        raise _fail("Codex CLI runtime closure is unsupported")
    package_name, vendor_target = target
    package_root = entrypoint.parent.parent
    if package_root.name != "codex" or package_root.parent.name != "@openai":
        raise _fail("Codex CLI runtime closure is unsupported")
    candidate = package_root / "node_modules" / "@openai" / package_name / "vendor" / vendor_target / "bin" / "codex"
    try:
        runtime = candidate.resolve(strict=True)
    except OSError as exc:
        raise _fail("Codex CLI runtime closure is unavailable") from exc
    if candidate.is_symlink() or not _is_direct_native_executable(runtime):
        raise _fail("Codex CLI runtime closure is unavailable")
    return runtime


def _probe_cli(binary: str, timeout: int) -> _CliProbe:
    path = shutil.which(binary) if os.path.sep not in binary else binary
    if not path:
        raise _fail("Codex CLI is unavailable")
    resolved = Path(path).resolve()
    try:
        entrypoint_sha256 = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise _fail("Codex CLI binary is unreadable") from exc
    runtime = _native_runtime_for(resolved)
    try:
        runtime_sha256 = hashlib.sha256(runtime.read_bytes()).hexdigest()
    except OSError as exc:
        raise _fail("Codex CLI runtime closure is unavailable") from exc
    version_result = _run_checked([str(resolved), "--version"], min(timeout, 15))
    if version_result.returncode != 0:
        raise _fail("Codex CLI version unavailable")
    version = ((version_result.stdout or "") + (version_result.stderr or "")).strip().splitlines()[0:1]
    if not version or len(version[0].encode("utf-8")) > _MAX_VERSION_BYTES:
        raise _fail("Codex CLI version unavailable")
    help_result = _run_checked([str(resolved), "exec", "--help"], min(timeout, 15))
    help_text = (help_result.stdout or "") + "\n" + (help_result.stderr or "")
    if help_result.returncode != 0 or len(help_text.encode("utf-8")) > _MAX_HELP_BYTES:
        raise _fail("Codex CLI help unavailable")
    missing = [flag for flag in _REQUIRED_HELP_FLAGS if flag not in help_text]
    if missing:
        raise _fail("Codex CLI isolation capability unavailable")
    return _CliProbe(binary, resolved, entrypoint_sha256, runtime, runtime_sha256, version[0][:160])


def _request_shape(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "argv": [
            "codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
            "--skip-git-repo-check", "--strict-config", "--json", "--model", config["model"],
            "--output-schema", "<strict-envelope>", "-c", "model_reasoning_effort=<requested-effort>",
            *chain.from_iterable(("--disable", feature) for feature in _DISABLED_FEATURES), "--enable", "skip_host_skill_discovery",
            "-c", "web_search=disabled", "-c", "features.web_search=false", "-c", "tools.web_search=false",
            "-c", "project_doc_max_bytes=0", "-c", "memories.use_memories=false", "-c", "memories.generate_memories=false", "-",
        ],
        "condition": "closed-book", "requested_model": config["model"], "requested_effort": config["effort"],
        "disabled_features": list(_DISABLED_FEATURES),
        "tools": [], "resume": False, "fresh_home": True, "fresh_cwd": True,
    }


def _assert_clean_feature_argv(argv: list[str]) -> None:
    """Reject contradictory feature pairs instead of relying on flag ordering."""

    pairs = [(argv[index], argv[index + 1]) for index in range(len(argv) - 1) if argv[index] in {"--enable", "--disable"}]
    names = [name for _flag, name in pairs]
    if len(names) != len(set(names)) or any(
        flag == "--enable" and name in _DISABLED_FEATURES for flag, name in pairs
    ):
        raise _fail("Codex feature argv is contradictory")


def build_closed_book_argv(
    binary: str,
    *,
    model: str,
    effort: str,
    response_schema_path: Path,
    transport_overrides: tuple[str, ...] = (),
) -> list[str]:
    """Build the shared native closed-book invocation.

    ``transport_overrides`` exists only for the local control fixture.  The
    production adapter passes none, so its hash-bound request shape remains
    separate from loopback transport plumbing.
    """

    argv = [
        binary, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
        "--skip-git-repo-check", "--strict-config", "--json", "--model", model,
        "--output-schema", str(response_schema_path), "-c", f'model_reasoning_effort="{effort}"',
    ]
    for feature in _DISABLED_FEATURES:
        argv.extend(["--disable", feature])
    argv.extend([
        "--enable", "skip_host_skill_discovery", "-c", 'web_search="disabled"',
        "-c", "features.web_search=false", "-c", "tools.web_search=false",
        "-c", "project_doc_max_bytes=0", "-c", "memories.use_memories=false",
        "-c", "memories.generate_memories=false", *transport_overrides, "-",
    ])
    _assert_clean_feature_argv(argv)
    return argv


def _settings_hash(config: Mapping[str, Any]) -> str:
    """Fingerprint only preflight-known controls, never a packet-derived schema."""

    return adapters.digest({"request": _request_shape(config), "response_schema": "packet-derived", "environment": sorted(_SAFE_CHILD_ENV | {"HOME", "CODEX_HOME"})})


def _load_control(options: NativeCodexOptions, probe: _CliProbe) -> dict[str, Any]:
    path = _private_file(options.private_env_path, CODEX_CONTROL_FILE, "native Codex control receipt")
    try:
        raw = path.read_bytes()
        receipt = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("native Codex control receipt is invalid") from exc
    if not isinstance(receipt, Mapping):
        raise _fail("native Codex control receipt is invalid")
    required = {"schema", "entrypoint_sha256", "native_runtime_sha256", "cli_version", "probe_implementation_sha256", "request_shape_sha256", "fresh_neutral_cwd", "fresh_home", "ignore_user_config", "ignore_rules", "ephemeral", "handler_controls", "handler_evidence"}
    if set(receipt) != required or receipt.get("schema") != "native-codex-control.v2":
        raise _fail("native Codex control receipt schema is invalid")
    if (
        receipt.get("entrypoint_sha256") != probe.entrypoint_sha256
        or receipt.get("native_runtime_sha256") != probe.native_runtime_sha256
        or receipt.get("cli_version") != probe.version
    ):
        raise _fail("native Codex control receipt identity drift")
    if receipt.get("probe_implementation_sha256") != _probe_implementation_sha256():
        raise _fail("native Codex control receipt probe implementation drift")
    if receipt.get("request_shape_sha256") != adapters.digest(_request_shape(options.config)):
        raise _fail("native Codex control receipt request shape drift")
    if any(receipt.get(key) is not True for key in ("fresh_neutral_cwd", "fresh_home", "ignore_user_config", "ignore_rules", "ephemeral")):
        raise _fail("native Codex control receipt isolation is incomplete")
    controls = receipt.get("handler_controls")
    if not isinstance(controls, Mapping) or set(controls) != _INERT_HANDLER_NAMES:
        raise _fail("native Codex handler controls are incomplete")
    if any(value != "inert" for value in controls.values()):
        raise _fail("native Codex handler control is not inert")
    handler_evidence = receipt.get("handler_evidence")
    if not isinstance(handler_evidence, Mapping) or set(handler_evidence) != _INERT_HANDLER_NAMES:
        raise _fail("native Codex handler evidence is incomplete")
    for evidence in handler_evidence.values():
        if not isinstance(evidence, Mapping):
            raise _fail("native Codex handler evidence is invalid")
        _exact_keys(evidence, _CONTROL_EVIDENCE_KEYS, "native Codex handler evidence")
        if evidence.get("source_kind") != "local-mock-capture" or evidence.get("request_shape_sha256") != receipt["request_shape_sha256"]:
            raise _fail("native Codex handler evidence provenance is invalid")
        raw_path = evidence.get("artifact_path")
        raw_hash = evidence.get("artifact_sha256")
        if not isinstance(raw_path, str) or not os.path.isabs(raw_path) or not isinstance(raw_hash, str) or re.fullmatch(r"[0-9a-f]{64}", raw_hash) is None:
            raise _fail("native Codex handler evidence provenance is invalid")
        artifact = Path(raw_path)
        try:
            if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size > _MAX_CONTROL_ARTIFACT_BYTES:
                raise _fail("native Codex handler evidence artifact is unavailable")
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != raw_hash:
                raise _fail("native Codex handler evidence artifact drift")
        except OSError as exc:
            raise _fail("native Codex handler evidence artifact is unavailable") from exc
    return dict(receipt)


def _probe_implementation_sha256() -> str:
    """Bind a receipt to the local verifier that produced its mock evidence."""

    try:
        return hashlib.sha256(Path(__file__).with_name("codex_controls.py").read_bytes()).hexdigest()
    except OSError as exc:
        raise _fail("native Codex control probe implementation is unavailable") from exc


def _validate_condition(condition: str, sources_url: str | None) -> None:
    if condition == "sources":
        raise _fail("native Codex Sources is unsupported until MCP isolation is proven")
    if condition != "closed-book":
        raise _fail("condition must be closed-book")
    if sources_url is not None:
        raise _fail("closed-book does not accept a Sources URL")


def preflight_codex(config: Mapping[str, Any], condition: str, sources_url: str | None = None, *, private_env_path: str | os.PathLike[str] | Path | None = None) -> dict[str, Any]:
    """Verify only installed surface and private, mock-captured handler evidence."""

    _validate_condition(condition, sources_url)
    options = validate_options(config, private_env_path=private_env_path)
    probe = _probe_cli(options.binary, options.config["timeout_seconds"])
    receipt = _load_control(options, probe)
    return {
        "schema": CODEX_CAPABILITY_SCHEMA, "adapter": CODEX_ADAPTER, "condition": condition,
        "requested_model": options.config["model"], "requested_model_alias": options.config["model"],
        "requested_effort": options.config["effort"], "accepted_effort": "unknown",
        "effective_effort": "unknown", "effective_backend_model": "unknown", "account_identity": "unknown",
        "tools_sha256": adapters.digest([]), "corpus_id_sha256": None, "capability": "native-codex-isolated-controls",
        "cli_version": probe.version, "version_observed": probe.version,
        "entrypoint_sha256": probe.entrypoint_sha256, "native_runtime_sha256": probe.native_runtime_sha256,
        "settings_sha256": _settings_hash(options.config), "request_shape_sha256": adapters.digest(_request_shape(options.config)),
        "control_receipt_sha256": adapters.digest(receipt),
        "tool_schema_sha256": adapters.digest([]), "mcp_server_identity_sha256": None,
        "max_output_tokens_configured": options.config["max_output_tokens"], "max_output_tokens_effective": "unknown",
    }


def _child_env(home: Path, auth_bytes: bytes) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _SAFE_CHILD_ENV}
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(home / "codex-home")
    codex_home = Path(env["CODEX_HOME"])
    codex_home.mkdir(mode=0o700)
    destination = codex_home / CODEX_AUTH_FILE
    destination.write_bytes(auth_bytes)
    destination.chmod(0o600)
    return env


def _terminate_process_group(process: subprocess.Popen[bytes]) -> bool:
    """Kill and reap the POSIX process group created for one CLI invocation."""

    # The group can retain a descendant after its leader has already exited.
    # Always address the original group before falling back to its leader.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


def _bounded_capture(
    output: Mapping[str, bytearray],
    truncated: Mapping[str, bool],
    returncode: int | None,
    *,
    incomplete_io: list[str],
) -> tuple[dict[str, Any], list[str]]:
    """Return strict text output or bounded raw bytes for decode failures."""

    capture: dict[str, Any] = {
        "stdout_truncated": truncated["stdout"],
        "stderr_truncated": truncated["stderr"],
        "returncode": returncode,
        "io_incomplete": bool(incomplete_io),
        "incomplete_io": incomplete_io,
    }
    invalid_streams: list[str] = []
    for name in ("stdout", "stderr"):
        raw = bytes(output[name])
        try:
            capture[name] = raw.decode("utf-8")
        except UnicodeDecodeError:
            invalid_streams.append(name)
            capture[f"{name}_raw_base64"] = base64.b64encode(raw).decode("ascii")
    return capture, invalid_streams


def _close_selector_stream(selector: selectors.BaseSelector, stream: Any) -> None:
    """Release one unbuffered Popen-owned stream after selector unregistering."""

    try:
        selector.unregister(stream)
    except KeyError:
        pass
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _run_process(argv: list[str], *, cwd: Path, env: Mapping[str, str], prompt: str, timeout: int, evidence: Callable[[str, Any], None] | None) -> subprocess.CompletedProcess[str]:
    """Run one CLI process while bounding both streams and the input deadline."""

    if timeout <= 0:
        raise _fail("Codex CLI timeout must be positive")
    try:
        process = subprocess.Popen(argv, cwd=str(cwd), env=dict(env), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, bufsize=0, start_new_session=True)
    except OSError as exc:
        raise _fail("Codex CLI invocation failed") from exc
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    stdin_fd, stdout_fd, stderr_fd = process.stdin.fileno(), process.stdout.fileno(), process.stderr.fileno()
    for fd in (stdin_fd, stdout_fd, stderr_fd):
        os.set_blocking(fd, False)
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    prompt_bytes = prompt.encode("utf-8")
    prompt_offset = 0
    if prompt_bytes:
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        _close_selector_stream(selector, process.stdin)
    timed_out = False
    overflow = False
    stream_error: str | None = None
    while selector.get_map() or process.poll() is None:
        if overflow or stream_error is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            events = selector.select(timeout=min(remaining, 0.05))
        except OSError:
            stream_error = "selector"
            break
        for key, _mask in events:
            name = key.data
            fd = key.fd
            if name == "stdin":
                try:
                    written = os.write(fd, prompt_bytes[prompt_offset:prompt_offset + 65_536])
                except (BlockingIOError, InterruptedError):
                    continue
                except (BrokenPipeError, OSError):
                    _close_selector_stream(selector, key.fileobj)
                    continue
                prompt_offset += written
                if prompt_offset == len(prompt_bytes):
                    _close_selector_stream(selector, key.fileobj)
                continue
            try:
                chunk = os.read(fd, 65_536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                stream_error = name
                break
            if not chunk:
                _close_selector_stream(selector, key.fileobj)
                continue
            remaining_output = _MAX_OUTPUT_BYTES - len(output[name])
            if remaining_output > 0:
                output[name].extend(chunk[:remaining_output])
            if len(chunk) > remaining_output:
                truncated[name] = True
                overflow = True
                break
    incomplete_io = [str(item.data) for item in selector.get_map().values()]
    reaped = True
    if overflow or timed_out or stream_error is not None:
        reaped = _terminate_process_group(process)
    for key in list(selector.get_map().values()):
        _close_selector_stream(selector, key.fileobj)
    selector.close()
    capture, invalid_streams = _bounded_capture(
        output, truncated, process.returncode, incomplete_io=incomplete_io,
    )
    if invalid_streams:
        capture["invalid_utf8_streams"] = invalid_streams
    if not reaped:
        if evidence is not None:
            evidence("cli_io_incomplete", capture)
        raise _fail("Codex CLI process cleanup is incomplete")
    if overflow:
        if evidence is not None:
            evidence("cli_output_overflow", capture)
        raise _fail("Codex CLI output exceeds limit")
    if timed_out:
        if evidence is not None:
            evidence("cli_timeout", capture)
        raise _fail("Codex CLI timeout")
    if stream_error is not None:
        if evidence is not None:
            evidence("cli_stream_error", capture)
        raise _fail("Codex CLI stream read failed")
    if invalid_streams:
        if evidence is not None:
            evidence("cli_invalid_utf8", capture)
        raise _fail("Codex CLI emitted invalid UTF-8")
    return subprocess.CompletedProcess(argv, process.returncode, stdout=capture["stdout"], stderr=capture["stderr"])


def _usage(value: Any) -> int | None:
    """Accept only concrete non-negative CLI token counters."""

    return value if type(value) is int and value >= 0 else None


def _parse_events(stdout: str, packet: Mapping[str, Any]) -> _ParsedCodexEvents:
    """Parse the installed ``codex exec --json`` protocol seen in mock captures.

    The observed stream supplies a thread id and selected usage counters.  It
    does not disclose a backend model, accepted/effective effort, or a tool
    surface.  Those values must therefore remain unknown rather than being
    manufactured from the requested route.  Closed-book tool safety comes from
    the separately hash-bound handler receipt; any emitted non-message item is
    nevertheless rejected as a runtime policy violation.
    """
    if len(stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise _fail("Codex CLI output exceeds limit")
    session: str | None = None
    turn_started = False
    turn_completed = False
    messages: list[str] = []
    usage: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None, "cost_usd": None}
    for index, line in enumerate(stdout.splitlines()):
        try:
            event = adapters._strict_json_loads(line)
        except adapters.AdapterError as exc:
            raise _fail("Codex CLI emitted invalid JSONL") from exc
        if not isinstance(event, Mapping) or not isinstance(event.get("type"), str):
            raise _fail("Codex CLI emitted invalid event")
        event_type = event["type"]
        if event_type == "thread.started":
            value = event.get("thread_id") or event.get("session_id")
            if index != 0 or not isinstance(value, str) or _SESSION_RE.fullmatch(value) is None or session is not None:
                raise _fail("Codex CLI session identity is invalid")
            session = value
        elif event_type == "turn.started":
            if session is None or turn_started or turn_completed:
                raise _fail("Codex CLI turn lifecycle is invalid")
            turn_started = True
        elif event_type == "item.completed":
            item = event.get("item")
            if turn_completed or not isinstance(item, Mapping):
                raise _fail("Codex CLI item event is invalid")
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                if not turn_started:
                    raise _fail("Codex CLI response preceded turn start")
                messages.append(item["text"])
            elif item.get("type") == "error" and isinstance(item.get("message"), str):
                # Captures show bootstrap warnings represented as item errors.
                # They are acceptable only after the session is known and before
                # a turn begins; an in-turn error cannot be hidden as bootstrap.
                if session is None or turn_started:
                    raise _fail("Codex CLI reported an item error during a turn")
                continue
            elif item.get("type") in {"function_call", "custom_tool_call", "mcp_call", "tool_call"}:
                raise _fail("Codex CLI tool event violates closed-book policy")
            else:
                raise _fail("Codex CLI emitted non-message item")
        elif event_type == "turn.completed":
            raw_usage = event.get("usage")
            if not turn_started or turn_completed or not isinstance(raw_usage, Mapping):
                raise _fail("Codex CLI usage is unobserved")
            usage = {"input_tokens": _usage(raw_usage.get("input_tokens")), "output_tokens": _usage(raw_usage.get("output_tokens")), "total_tokens": None, "cost_usd": None}
            if usage["input_tokens"] is None or usage["output_tokens"] is None:
                raise _fail("Codex CLI usage is unobserved")
            turn_completed = True
        elif event_type in {"turn.failed", "error"}:
            raise _fail("Codex CLI reported a failed turn")
        else:
            raise _fail("Codex CLI emitted unsupported event")
    if session is None or not turn_started or not turn_completed or not messages:
        raise _fail("Codex CLI omitted required session, usage, or response")
    answer_content = "".join(messages)
    try:
        payload = adapters._strict_json_loads(answer_content)
        responses = adapters._extract_responses(payload, packet)
    except adapters.AdapterError as exc:
        return _ParsedCodexEvents(
            responses=None,
            session_id=session,
            usage=usage,
            answer_failure_reason=str(exc) or "Codex CLI response is invalid",
            answer_content=answer_content,
        )
    return _ParsedCodexEvents(
        responses=responses,
        session_id=session,
        usage=usage,
        answer_failure_reason=None,
        answer_content=answer_content,
    )


def run_codex(packet: Mapping[str, Any], config: Mapping[str, Any], condition: str, *, sources_url: str | None, prompt: str, evidence: Callable[[str, Any], None] | None = None, private_env_path: str | os.PathLike[str] | Path | None = None) -> dict[str, Any]:
    """Run one fresh Codex session, preserving unredacted CLI events via callback."""

    _validate_condition(condition, sources_url)
    if not isinstance(prompt, str) or not prompt:
        raise _fail("Codex prompt must be a nonempty string")
    options = validate_options(config, private_env_path=private_env_path)
    probe = _probe_cli(options.binary, options.config["timeout_seconds"])
    control_receipt = _load_control(options, probe)
    control_receipt_sha256 = adapters.digest(control_receipt)
    auth_bytes = _sanitized_chatgpt_auth(options.private_env_path)
    with tempfile.TemporaryDirectory(prefix="codex-eval-home-") as home_temp, tempfile.TemporaryDirectory(prefix="codex-eval-cwd-") as cwd_temp:
        home, cwd = Path(home_temp), Path(cwd_temp)
        home.chmod(0o700)
        cwd.chmod(0o700)
        response_schema = adapters.response_schema(packet)
        response_schema_sha256 = adapters.digest(response_schema)
        schema_path = cwd / "response-schema.json"
        schema_path.write_text(adapters.canonical(response_schema), encoding="utf-8")
        schema_path.chmod(0o600)
        env = _child_env(home, auth_bytes)
        argv = build_closed_book_argv(
            str(probe.binary_path),
            model=options.config["model"],
            effort=options.config["effort"],
            response_schema_path=schema_path,
        )
        if evidence is not None:
            evidence("cli_invocation", {"argv": argv, "cwd": str(cwd), "env_keys": sorted(env), "condition": condition, "settings_sha256": _settings_hash(options.config), "response_schema_sha256": response_schema_sha256, "control_receipt_sha256": control_receipt_sha256, "entrypoint_sha256": probe.entrypoint_sha256, "native_runtime_sha256": probe.native_runtime_sha256})
        started = time.monotonic()
        completed = _run_process(argv, cwd=cwd, env=env, prompt=prompt, timeout=options.config["timeout_seconds"], evidence=evidence)
        elapsed = time.monotonic() - started
        if evidence is not None:
            evidence("cli_result", {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr})
        if completed.returncode != 0:
            raise _fail("Codex CLI invocation failed")
        parsed = _parse_events(completed.stdout or "", packet)
        if parsed.answer_failure_reason is not None:
            responses = {str(item["id"]): None for item in packet["items"]}
            if evidence is not None:
                evidence(
                    "candidate_answer_outcome",
                    {
                        "failure_reason": CANDIDATE_RESPONSE_ERROR,
                        "parser_reason": parsed.answer_failure_reason,
                        "answer_content": parsed.answer_content,
                        "session_id": parsed.session_id,
                        "tool_calls": 0,
                        "usage": dict(parsed.usage),
                    },
                )
        else:
            assert parsed.responses is not None
            responses = parsed.responses
    trial: dict[str, Any] = {"responses": responses, "identity": {"adapter": CODEX_ADAPTER, "harness": CODEX_HARNESS, "model": options.config["model"], "provider": CODEX_PROVIDER, "account_identity": "unknown", "session_id": parsed.session_id, "control_receipt_sha256": control_receipt_sha256, "requested_model": options.config["model"], "requested_model_alias": options.config["model"], "effective_model": "unknown", "effective_backend_model": "unknown", "requested_effort": options.config["effort"], "accepted_effort": "unknown", "effective_effort": "unknown", "cli_version": probe.version, "version_observed": probe.version, "entrypoint_sha256": probe.entrypoint_sha256, "native_runtime_sha256": probe.native_runtime_sha256, "tool_schema_sha256": adapters.digest([]), "corpus_id_sha256": None, "mcp_server_identity_sha256": None, "max_output_tokens_configured": options.config["max_output_tokens"], "max_output_tokens_effective": "unknown", "settings_sha256": _settings_hash(options.config), "request_shape_sha256": adapters.digest(_request_shape(options.config)), "response_schema_sha256": response_schema_sha256}, "metrics": {"elapsed_seconds": elapsed, **parsed.usage, "tool_calls": 0}}
    if parsed.answer_failure_reason is not None:
        trial.update(status="failed", failure_reason=CANDIDATE_RESPONSE_ERROR)
    return trial


preflight = preflight_codex
run = run_codex

__all__ = ["CODEX_ADAPTER", "CODEX_PROVIDER", "CodexAdapterError", "NativeCodexOptions", "build_closed_book_argv", "preflight", "preflight_codex", "run", "run_codex", "validate_codex_config", "validate_config", "validate_options"]
