"""Fail-closed native Kimi Code adapter.

This module is intentionally separate from the shared adapter registry.  It
implements the installed Kimi Code 0.41 v2 launch controls without importing
or copying a user's Kimi home.  A caller supplies a private provisioning
directory for an already-authorized OAuth subscription; the adapter copies
only the selected managed provider, model alias, and credential token into a
fresh owner-only home for one attempt.

The public functions are:

``validate_kimi_config`` / ``validate_config`` / ``validate_options``
    Validate native Kimi options and the private provisioning path boundary.
    ``validate_kimi_config`` only checks Kimi-specific fields; ``validate_config``
    is the full adapter entry point used by the shared runner.  The path is a
    call argument so it never enters a persisted evaluator config or receipt.
``preflight_kimi`` / ``preflight``
    Verify the installed CLI surface and the selected native config/catalog
    without starting a model turn.
``run_kimi``
    Run one fresh Kimi session and return the same trial shape as
    :func:`ukrainian_llm_eval.adapters.run_claude`.

No provider-generated model refresh, API-key fallback, alias expansion, or
credential login flow is attempted here.  Missing or ambiguous provisioning
is an explicit readiness failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import adapters
from .mcp_proxy import REFERENCE_TOOLS

KIMI_ADAPTER = "kimi"
KIMI_PROVIDER = "managed:kimi-code"
KIMI_SCHEMA = "zno-nmt.config.v1"
KIMI_CAPABILITY_SCHEMA = "zno-nmt.capability.v1"
KIMI_HARNESS = "kimi-cli"
KIMI_LEGACY_ENV = "KIMI_CODE_LEGACY_FLAG"
KIMI_HOME_ENV = "KIMI_CODE_HOME"
KIMI_EFFORT_ENV = "KIMI_MODEL_THINKING_EFFORT"
KIMI_OUTPUT_ENV = "KIMI_MODEL_MAX_COMPLETION_TOKENS"
KIMI_DEFAULT_COM_URL = "https://api.kimi.com/coding/v1"
KIMI_DEFAULT_GLOBAL_URL = "https://api.kimi.ai/coding/v1"
KIMI_COM_OAUTH_HOST = "https://auth.kimi.com"
KIMI_GLOBAL_OAUTH_HOST = "https://auth.kimi.ai"
KIMI_MODEL_ALIAS_PREFIX = "kimi-code/"
KIMI_OAUTH_KEY = "oauth/kimi-code"
KIMI_OAUTH_SCOPED_RE = re.compile(r"^oauth/kimi-code-env-[0-9a-f]{16}$")
KIMI_MODEL_ALIAS_RE = re.compile(r"^kimi-code/[^/\s\x00-\x1f\x7f]+$")
KIMI_OAUTH_KEY_RE = re.compile(r"^oauth/kimi-code(?:-env-[0-9a-f]{16})?$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SAFE_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]*$")
_TOKEN_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_STREAM_BYTES = 2_000_000
_MAX_VERSION_BYTES = 16_384
_MAX_HELP_BYTES = 256_000
_MAX_PRIVATE_CONFIG_BYTES = 1_000_000
_MAX_PRIVATE_TOKEN_BYTES = 128_000
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
        "kimi_bin",
    }
)
_ALLOWED_PROVIDER_KEYS = frozenset({"type", "base_url", "api_key", "oauth"})
_ALLOWED_OAUTH_KEYS = frozenset({"storage", "key", "oauth_host"})
_ALLOWED_MODEL_KEYS = frozenset(
    {
        "provider",
        "model",
        "max_context_size",
        "max_input_size",
        "max_output_size",
        "capabilities",
        "display_name",
        "reasoning_key",
        "protocol",
        "adaptive_thinking",
        "support_efforts",
        "default_effort",
        "off_effort",
        "beta_api",
        "base_url",
    }
)
_COMMON_CHILD_ENV = frozenset(
    {
        "PATH",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "SHELL",
        "TERM",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


class KimiAdapterError(adapters.AdapterError):
    """A Kimi route or its isolation evidence violated the adapter contract."""


@dataclass(frozen=True)
class NativeKimiOptions:
    """Validated serialisable options plus a private, non-persisted source path."""

    config: dict[str, Any]
    private_env_path: Path
    binary: str


@dataclass(frozen=True)
class _Provisioning:
    provider: dict[str, Any]
    model: dict[str, Any]
    provider_id: str
    alias: str
    oauth: dict[str, Any]
    credential_path: Path
    token: dict[str, Any]
    native_config_sha256: str
    catalog_provider_sha256: str
    catalog_model_sha256: str


@dataclass(frozen=True)
class _CliProbe:
    binary: str
    binary_path: Path
    binary_sha256: str
    version: str


def _fail(message: str) -> KimiAdapterError:
    return KimiAdapterError(message)


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


def validate_kimi_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate only native Kimi fields, without reading credentials.

    The shared adapter validator owns the common schema, condition, and
    request-budget fields.  This helper intentionally accepts a partial
    mapping so that the shared validator can call it without a recursive
    dependency; the common-required ``model`` alias is checked here and the
    optional ``kimi_bin`` path is normalised here.
    """

    if not isinstance(config, Mapping):
        raise _fail("configuration must be an object")
    adapter = config.get("adapter")
    if adapter is not None and adapter != KIMI_ADAPTER:
        raise _fail("configuration adapter is not kimi")
    model = config.get("model")
    if model is None:
        raise _fail("configuration model is required")
    model = _nonempty_text(model, "configuration model")
    if not KIMI_MODEL_ALIAS_RE.fullmatch(model):
        raise _fail("configuration model must be an exact kimi-code alias")
    binary = config.get("kimi_bin", "kimi")
    binary = _nonempty_text(binary, "configuration kimi_bin", max_length=1024)
    return {**dict(config), "model": model, "kimi_bin": binary}


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the full serialisable Kimi route without reading credentials."""

    checked_native = validate_kimi_config(config)
    _require_exact_keys(checked_native, _ALLOWED_CONFIG_KEYS, "configuration")
    if checked_native.get("schema") != KIMI_SCHEMA:
        raise _fail("configuration schema mismatch")
    if checked_native.get("adapter") != KIMI_ADAPTER:
        raise _fail("configuration adapter is not kimi")
    model = checked_native["model"]
    effort = checked_native.get("effort")
    if effort is not None:
        effort = _nonempty_text(effort, "configuration effort", max_length=64)
    for field in ("timeout_seconds", "max_output_tokens", "max_tool_calls", "repeats"):
        _positive_int(checked_native.get(field), f"configuration {field}")
    tools = checked_native.get("tools")
    if not isinstance(tools, list) or any(
        not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", tool) or tool not in REFERENCE_TOOLS
        for tool in tools
    ):
        raise _fail("configuration tools must be allowlisted Sources MCP references")
    if len(tools) != len(set(tools)):
        raise _fail("configuration tools contains duplicates")
    corpus_id = checked_native.get("corpus_id")
    if corpus_id is not None:
        corpus_id = _nonempty_text(corpus_id, "configuration corpus_id", max_length=512)
    provider = checked_native.get("provider")
    if provider is not None:
        provider = _nonempty_text(provider, "configuration provider", max_length=256)
        if provider != KIMI_PROVIDER:
            raise _fail("configuration provider identity is contradictory")
    binary = checked_native["kimi_bin"]
    return {
        **dict(checked_native),
        "model": model,
        "effort": effort,
        "tools": list(tools),
        "corpus_id": corpus_id,
        "provider": provider,
        "kimi_bin": binary,
    }


def _private_root(value: str | os.PathLike[str] | Path | None) -> Path:
    if value is None:
        raise _fail("native Kimi provisioning is unresolved; private_env_path is required")
    if not isinstance(value, (str, os.PathLike, Path)):
        raise _fail("private_env_path must be a directory")
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
        raise _fail("private_env_path must be an absolute directory")
    path = Path(raw)
    try:
        if path.is_symlink() or not path.is_dir():
            raise _fail("private_env_path must be a directory")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _fail("private_env_path is unavailable") from exc
    _require_private_mode(resolved, "private_env_path")
    return resolved


def validate_options(
    config: Mapping[str, Any], *, private_env_path: str | os.PathLike[str] | Path | None
) -> NativeKimiOptions:
    """Validate config and the private provisioning boundary.

    ``private_env_path`` is deliberately required as a keyword-only runtime
    input.  It is never included in the returned capability or trial identity.
    """

    checked = validate_config(config)
    root = _private_root(private_env_path)
    return NativeKimiOptions(config=checked, private_env_path=root, binary=checked["kimi_bin"])


def _require_private_mode(path: Path, label: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise _fail(f"{label} is unavailable") from exc
    if mode & 0o077:
        raise _fail(f"{label} must be owner-only")


def _source_file(root: Path, relative: str, label: str) -> Path:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _fail(f"{label} is unavailable") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _fail(f"{label} escapes private_env_path") from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise _fail(f"{label} must be a regular file")
    _require_private_mode(resolved, label)
    return resolved


def _load_source_toml(root: Path) -> dict[str, Any]:
    config_path = _source_file(root, "config.toml", "private Kimi config")
    try:
        raw = config_path.read_bytes()
        if len(raw) > _MAX_PRIVATE_CONFIG_BYTES:
            raise _fail("private Kimi config exceeds limit")
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _fail("private Kimi config is invalid") from exc
    if not isinstance(parsed, dict):
        raise _fail("private Kimi config is invalid")
    return parsed


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(f"{label} must be an object")
    return dict(value)


def _safe_provider(provider: Mapping[str, Any], provider_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    record = _mapping(provider, "private Kimi provider")
    _require_exact_keys(record, _ALLOWED_PROVIDER_KEYS, "private Kimi provider")
    if record.get("type") != "kimi":
        raise _fail("private Kimi provider type is unsupported")
    if record.get("api_key") not in (None, ""):
        raise _fail("private Kimi provider API-key route is unsupported")
    base_url = record.get("base_url")
    if base_url is None:
        base_url = KIMI_DEFAULT_COM_URL
    base_url = _nonempty_text(base_url, "private Kimi provider base_url", max_length=256).rstrip("/")
    if base_url not in {KIMI_DEFAULT_COM_URL, KIMI_DEFAULT_GLOBAL_URL}:
        raise _fail("private Kimi provider base_url is not an approved managed route")
    oauth = _mapping(record.get("oauth"), "private Kimi provider oauth")
    _require_exact_keys(oauth, _ALLOWED_OAUTH_KEYS, "private Kimi provider oauth")
    if oauth.get("storage") != "file":
        raise _fail("private Kimi OAuth storage must be file")
    key = _nonempty_text(oauth.get("key"), "private Kimi OAuth key", max_length=128)
    if not KIMI_OAUTH_KEY_RE.fullmatch(key):
        raise _fail("private Kimi OAuth key is not approved")
    oauth_host = oauth.get("oauth_host")
    if oauth_host is None:
        oauth_host = KIMI_COM_OAUTH_HOST
    oauth_host = _nonempty_text(oauth_host, "private Kimi OAuth host", max_length=256).rstrip("/")
    if oauth_host not in {KIMI_COM_OAUTH_HOST, KIMI_GLOBAL_OAUTH_HOST}:
        raise _fail("private Kimi OAuth host is not approved")
    if base_url == KIMI_DEFAULT_COM_URL and oauth_host != KIMI_COM_OAUTH_HOST:
        raise _fail("private Kimi region configuration is contradictory")
    if base_url == KIMI_DEFAULT_GLOBAL_URL and oauth_host != KIMI_GLOBAL_OAUTH_HOST:
        raise _fail("private Kimi region configuration is contradictory")
    if base_url == KIMI_DEFAULT_COM_URL and key != KIMI_OAUTH_KEY:
        raise _fail("private Kimi mainland OAuth key is contradictory")
    if base_url == KIMI_DEFAULT_GLOBAL_URL and not KIMI_OAUTH_SCOPED_RE.fullmatch(key):
        raise _fail("private Kimi global OAuth key is contradictory")
    safe_provider = {"type": "kimi", "base_url": base_url, "oauth": {"storage": "file", "key": key, "oauth_host": oauth_host}}
    return safe_provider, {"storage": "file", "key": key, "oauth_host": oauth_host}


def _safe_model(
    model: Mapping[str, Any],
    alias: str,
    provider_id: str,
    provider_base_url: str,
) -> dict[str, Any]:
    record = _mapping(model, "private Kimi model alias")
    _require_exact_keys(record, _ALLOWED_MODEL_KEYS, "private Kimi model alias")
    if record.get("provider") != provider_id:
        raise _fail("private Kimi model provider identity is contradictory")
    if not KIMI_MODEL_ALIAS_RE.fullmatch(alias):
        raise _fail("private Kimi model alias is not approved")
    model_id = _nonempty_text(record.get("model"), "private Kimi model id", max_length=256)
    if model_id != alias.removeprefix(KIMI_MODEL_ALIAS_PREFIX):
        raise _fail("private Kimi model alias and model id are contradictory")
    max_context = _positive_int(record.get("max_context_size"), "private Kimi max_context_size")
    safe: dict[str, Any] = {
        "provider": provider_id,
        "model": model_id,
        "max_context_size": max_context,
    }
    for field in (
        "max_input_size",
        "max_output_size",
    ):
        if field in record:
            safe[field] = _positive_int(record[field], f"private Kimi {field}")
    if "max_input_size" in safe and safe["max_input_size"] > max_context:
        raise _fail("private Kimi max_input_size exceeds max_context_size")
    if "capabilities" in record:
        capabilities = record["capabilities"]
        if not isinstance(capabilities, list) or any(not isinstance(item, str) or not item or not _SAFE_TEXT_RE.fullmatch(item) for item in capabilities):
            raise _fail("private Kimi capabilities are invalid")
        if len(capabilities) != len(set(capabilities)):
            raise _fail("private Kimi capabilities contain duplicates")
        safe["capabilities"] = list(capabilities)
    for field in ("display_name", "reasoning_key", "default_effort", "off_effort"):
        if field in record:
            safe[field] = _nonempty_text(record[field], f"private Kimi {field}", max_length=256)
    if "protocol" in record:
        if record["protocol"] not in {"anthropic", "openai_responses"}:
            raise _fail("private Kimi model protocol is unsupported")
        safe["protocol"] = record["protocol"]
    for field in ("adaptive_thinking", "beta_api"):
        if field in record:
            if type(record[field]) is not bool:
                raise _fail(f"private Kimi {field} must be boolean")
            safe[field] = record[field]
    if "support_efforts" in record:
        efforts = record["support_efforts"]
        if not isinstance(efforts, list) or any(not isinstance(item, str) or not item or not _SAFE_TEXT_RE.fullmatch(item) for item in efforts):
            raise _fail("private Kimi support_efforts are invalid")
        if len(efforts) != len(set(efforts)):
            raise _fail("private Kimi support_efforts contain duplicates")
        safe["support_efforts"] = list(efforts)
    if "base_url" in record:
        model_base_url = _nonempty_text(record["base_url"], "private Kimi model base_url", max_length=256).rstrip("/")
        if model_base_url != provider_base_url:
            raise _fail("private Kimi model base_url contradicts managed provider")
        safe["base_url"] = model_base_url
    return safe


def _load_token(root: Path, oauth: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    key = str(oauth["key"])
    token_name = Path(key).name
    if not _TOKEN_NAME_RE.fullmatch(token_name):
        raise _fail("private Kimi OAuth key cannot be mapped safely")
    token_path = _source_file(root, f"credentials/{token_name}.json", "private Kimi OAuth credential")
    try:
        raw = token_path.read_bytes()
        if len(raw) > _MAX_PRIVATE_TOKEN_BYTES:
            raise _fail("private Kimi OAuth credential exceeds limit")
        parsed = adapters._strict_json_loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, adapters.AdapterError, KimiAdapterError) as exc:
        raise _fail("private Kimi OAuth credential is invalid") from exc
    token = _mapping(parsed, "private Kimi OAuth credential")
    allowed = {"access_token", "refresh_token", "expires_at", "scope", "token_type", "expires_in"}
    _require_exact_keys(token, allowed, "private Kimi OAuth credential")
    for field in ("access_token", "refresh_token"):
        _nonempty_text(token.get(field), f"private Kimi OAuth {field}", max_length=16_384)
    for field in ("scope", "token_type"):
        if field in token and not isinstance(token[field], str):
            raise _fail(f"private Kimi OAuth {field} is invalid")
    for field in ("expires_at", "expires_in"):
        if field in token and (
            isinstance(token[field], bool) or not isinstance(token[field], (int, float)) or token[field] < 0
        ):
            raise _fail(f"private Kimi OAuth {field} is invalid")
    return token_path, token


def _load_provisioning(options: NativeKimiOptions) -> _Provisioning:
    source = _load_source_toml(options.private_env_path)
    providers = _mapping(source.get("providers"), "private Kimi providers")
    provider_id = KIMI_PROVIDER
    provider_raw = providers.get(provider_id)
    if not isinstance(provider_raw, Mapping):
        raise _fail("private Kimi managed provider is unavailable")
    provider, oauth = _safe_provider(provider_raw, provider_id)
    models = _mapping(source.get("models"), "private Kimi models")
    alias = options.config["model"]
    model_raw = models.get(alias)
    if not isinstance(model_raw, Mapping):
        raise _fail("private Kimi requested model alias is unavailable")
    model = _safe_model(model_raw, alias, provider_id, provider["base_url"])
    if options.config["effort"] is not None:
        supported = model.get("support_efforts")
        if not isinstance(supported, list) or options.config["effort"] not in supported:
            raise _fail("private Kimi requested effort support is unresolved")
    max_output_size = model.get("max_output_size")
    if max_output_size is not None and options.config["max_output_tokens"] > max_output_size:
        raise _fail("configuration max_output_tokens exceeds private Kimi model limit")
    token_path, token = _load_token(options.private_env_path, oauth)
    safe_config = {
        "provider_id": provider_id,
        "provider": provider,
        "model_alias": alias,
        "model": model,
        "requested_effort": options.config["effort"],
    }
    return _Provisioning(
        provider=provider,
        model=model,
        provider_id=provider_id,
        alias=alias,
        oauth=oauth,
        credential_path=token_path,
        token=token,
        native_config_sha256=adapters.digest(safe_config),
        catalog_provider_sha256=adapters.digest(provider),
        catalog_model_sha256=adapters.digest(model),
    )


def _resolve_binary(binary: str) -> Path:
    candidate = Path(binary)
    if candidate.is_absolute():
        resolved = candidate
    else:
        located = shutil.which(binary)
        if located is None:
            raise _fail("Kimi CLI binary is unavailable")
        resolved = Path(located)
    if not str(resolved):
        raise _fail("Kimi CLI binary is unavailable")
    try:
        resolved = resolved.resolve(strict=True)
    except OSError as exc:
        raise _fail("Kimi CLI binary is unavailable") from exc
    if not resolved.is_file():
        raise _fail("Kimi CLI binary is unavailable")
    try:
        binary_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise _fail("Kimi CLI binary is unreadable") from exc
    if not binary_hash:
        raise _fail("Kimi CLI binary is unavailable")
    return resolved


def _probe_env(root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _COMMON_CHILD_ENV}
    env["HOME"] = str(root)
    env[KIMI_HOME_ENV] = str(root)
    env[KIMI_LEGACY_ENV] = "0"
    return env


def _probe_cli(binary: str, timeout: int) -> _CliProbe:
    path = _resolve_binary(binary)
    with tempfile.TemporaryDirectory(prefix="kimi-probe-") as temp:
        root = Path(temp)
        root.chmod(0o700)
        env = _probe_env(root)
        try:
            version_result = subprocess.run(
                [str(path), "--version"],
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=min(timeout, 15),
            )
            help_result = subprocess.run(
                [str(path), "--help"],
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=min(timeout, 15),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _fail("Kimi CLI capability probe failed") from exc
    if version_result.returncode != 0:
        raise _fail("Kimi CLI version probe failed")
    version_text = (version_result.stdout or "")[:_MAX_VERSION_BYTES]
    version_lines = [line.strip() for line in version_text.splitlines() if line.strip()]
    if not version_lines:
        raise _fail("Kimi CLI version is unavailable")
    version = _nonempty_text(version_lines[0], "Kimi CLI version", max_length=160)
    if help_result.returncode != 0:
        raise _fail("Kimi CLI help probe failed")
    help_text = ((help_result.stdout or "") + "\n" + (help_result.stderr or ""))[:_MAX_HELP_BYTES]
    required = ("--model", "--prompt", "--output-format", "stream-json", "--skills-dir", "--agent-file")
    if any(flag.casefold() not in help_text.casefold() for flag in required):
        raise _fail("Kimi CLI isolation capability unavailable")
    return _CliProbe(
        binary=binary,
        binary_path=path,
        binary_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        version=version,
    )


def _validate_condition(config: Mapping[str, Any], condition: str, sources_url: str | None) -> None:
    if condition not in {"closed-book", "sources"}:
        raise _fail("condition must be closed-book or sources")
    if condition == "sources":
        if not config["tools"] or not isinstance(sources_url, str) or not sources_url.strip():
            raise _fail("sources condition requires nonempty sources URL and tools")
        if any(ord(char) < 32 for char in sources_url):
            raise _fail("sources URL contains control characters")
        try:
            parsed = urllib.parse.urlsplit(sources_url)
        except ValueError as exc:
            raise _fail("sources URL is invalid") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise _fail("sources URL must be an HTTP(S) URL")


def _profile_text(condition: str, tools: list[str]) -> str:
    qualified = [f"mcp__sources__{tool}" for tool in tools] if condition == "sources" else []
    tool_json = json.dumps(qualified, ensure_ascii=False, separators=(",", ":"))
    return (
        "---\n"
        "name: ukrainian-llm-eval\n"
        "description: Isolated Ukrainian evaluation candidate\n"
        f"tools: {tool_json}\n"
        "subagents: []\n"
        "---\n"
        "You are an isolated candidate in a bounded Ukrainian evaluation. "
        "Do not use commands, files, web access, memory, or unlisted tools. "
        "Return only the JSON requested by the evaluation prompt.\n"
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        if any(not isinstance(item, (str, int, bool)) for item in value):
            raise _fail("private Kimi config contains unsupported values")
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise _fail("private Kimi config contains unsupported values")


def _write_safe_config(home: Path, provisioning: _Provisioning, effort: str | None) -> Path:
    provider = provisioning.provider
    model = provisioning.model
    lines = [
        "telemetry = false",
        f"default_provider = {_toml_string(provisioning.provider_id)}",
        f"default_model = {_toml_string(provisioning.alias)}",
        "",
        f"[providers.{_toml_string(provisioning.provider_id)}]",
        f"type = {_toml_string(provider['type'])}",
        f"base_url = {_toml_string(provider['base_url'])}",
        "api_key = \"\"",
        "",
        f"[providers.{_toml_string(provisioning.provider_id)}.oauth]",
        f"storage = {_toml_string(provisioning.oauth['storage'])}",
        f"key = {_toml_string(provisioning.oauth['key'])}",
        f"oauth_host = {_toml_string(provisioning.oauth['oauth_host'])}",
        "",
        f"[models.{_toml_string(provisioning.alias)}]",
    ]
    for key, value in model.items():
        lines.append(f"{key} = {_toml_value(value)}")
    if effort is not None:
        lines.extend(["", "[thinking]", f"effort = {_toml_string(effort)}"])
    config_path = home / "config.toml"
    try:
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        config_path.chmod(0o600)
    except OSError as exc:
        raise _fail("isolated Kimi config could not be written") from exc
    return config_path


def _write_safe_token(home: Path, provisioning: _Provisioning) -> Path:
    token_name = Path(provisioning.oauth["key"]).name
    destination_dir = home / "credentials"
    destination = destination_dir / f"{token_name}.json"
    try:
        destination_dir.mkdir(mode=0o700)
        destination_dir.chmod(0o700)
        destination.write_text(
            json.dumps(provisioning.token, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        destination.chmod(0o600)
    except (OSError, adapters.AdapterError) as exc:
        raise _fail("isolated Kimi credential could not be provisioned") from exc
    return destination


def _write_sources_mcp(home: Path, sources_url: str, tools: list[str], max_tool_calls: int) -> Path:
    proxy = Path(__file__).with_name("mcp_proxy.py").resolve()
    if not proxy.is_file() or not Path(sys.executable).is_file():
        raise _fail("Sources MCP proxy unavailable")
    payload = {
        "mcpServers": {
            "sources": {
                "transport": "stdio",
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
    path = home / "mcp.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        raise _fail("isolated Sources MCP config could not be written") from exc
    return path


def _write_profile_and_skills(cwd: Path, condition: str, tools: list[str]) -> tuple[Path, Path]:
    profile = cwd / "agent.md"
    skills = cwd / "empty-skills"
    try:
        profile.write_text(_profile_text(condition, tools), encoding="utf-8")
        profile.chmod(0o600)
        skills.mkdir(mode=0o700)
        skills.chmod(0o700)
    except OSError as exc:
        raise _fail("isolated Kimi profile could not be written") from exc
    return profile, skills


def _child_env(home: Path, max_output_tokens: int, effort: str | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _COMMON_CHILD_ENV}
    env["HOME"] = str(home)
    env[KIMI_HOME_ENV] = str(home)
    env[KIMI_LEGACY_ENV] = "0"
    env[KIMI_OUTPUT_ENV] = str(max_output_tokens)
    if effort is not None:
        env[KIMI_EFFORT_ENV] = effort
    return env


def _assert_neutral_cwd(cwd: Path) -> None:
    current = cwd.resolve()
    while True:
        if (current / ".git").exists() or (current / ".mcp.json").exists() or (current / ".kimi-code" / "mcp.json").exists():
            raise _fail("isolated Kimi cwd has ambient project controls")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _run_kimi_process(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    prompt: str,
    timeout: int,
    evidence: Callable[[str, Any], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one process group and retain timeout diagnostics in private evidence."""

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
        raise _fail("Kimi CLI timeout") from exc
    except OSError as exc:
        raise _fail("Kimi CLI invocation failed") from exc
    return subprocess.CompletedProcess(argv, process.returncode, stdout=stdout, stderr=stderr)


def _check_event_identity(event: Mapping[str, Any], requested_alias: str) -> None:
    for key in ("model", "model_alias", "requested_model"):
        value = event.get(key)
        if value is not None and value != requested_alias:
            raise _fail("CLI model identity drift")
    for key in ("provider", "backend_model", "effective_model", "effective_backend_model"):
        value = event.get(key)
        if value not in (None, "", "unknown", requested_alias, KIMI_PROVIDER):
            raise _fail("CLI provider identity drift")
    if event.get("fallback") not in (None, False):
        raise _fail("CLI fallback detected")
    if event.get("fallback_model") not in (None, ""):
        raise _fail("CLI fallback detected")


def _usage_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return value


def _parse_stream_json(
    stdout: str,
    packet: Mapping[str, Any],
    allowed_tools: set[str],
    max_tools: int,
    requested_alias: str,
    expected_version: str | None = None,
) -> tuple[dict[str, Any], str, str, int, dict[str, int | float | None]]:
    """Parse the exact Kimi stream-json subset and reject unsupported events."""

    if len(stdout.encode("utf-8", errors="strict")) > _MAX_STREAM_BYTES:
        raise _fail("Kimi CLI output exceeds limit")
    version: str | None = None
    session_id: str | None = None
    contents: list[str] = []
    tool_calls: dict[str, str] = {}
    tool_results: set[str] = set()
    usage: dict[str, int | float | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
    }
    for index, line in enumerate(stdout.splitlines()):
        if not line.strip():
            raise _fail("CLI emitted blank stream JSON line")
        try:
            event = adapters._strict_json_loads(line)
        except adapters.AdapterError as exc:
            raise _fail("CLI emitted invalid stream JSON") from exc
        if not isinstance(event, Mapping):
            raise _fail("CLI emitted invalid stream event")
        _check_event_identity(event, requested_alias)
        role = event.get("role")
        event_type = event.get("type")
        if index == 0 and not (role == "meta" and event_type == "system.version"):
            raise _fail("CLI version event missing")
        if role == "meta":
            if event_type == "system.version":
                if version is not None:
                    raise _fail("CLI emitted duplicate version event")
                value = _nonempty_text(event.get("version"), "CLI version", max_length=160)
                version = value
                continue
            if event_type == "session.resume_hint":
                value = _nonempty_text(event.get("session_id"), "CLI session id", max_length=256)
                if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value) is None:
                    raise _fail("CLI session id is invalid")
                if session_id is not None and value != session_id:
                    raise _fail("CLI session identity drift")
                session_id = value
                continue
            if event_type == "turn.step.retrying":
                raise _fail("CLI retry event rejected")
            raise _fail("CLI meta event rejected")
        if role == "assistant":
            content = event.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise _fail("CLI assistant content is invalid")
                contents.append(content)
            raw_calls = event.get("tool_calls")
            if raw_calls is not None:
                if not isinstance(raw_calls, list):
                    raise _fail("CLI tool call surface is malformed")
                for raw_call in raw_calls:
                    call = _mapping(raw_call, "CLI tool call")
                    _require_exact_keys(call, {"type", "id", "function"}, "CLI tool call")
                    if call.get("type") != "function":
                        raise _fail("CLI tool call surface is malformed")
                    call_id = _nonempty_text(call.get("id"), "CLI tool call id", max_length=256)
                    function = _mapping(call.get("function"), "CLI tool call function")
                    _require_exact_keys(function, {"name", "arguments"}, "CLI tool call function")
                    name = _nonempty_text(function.get("name"), "CLI tool call name", max_length=256)
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        raise _fail("CLI tool call arguments are invalid")
                    try:
                        parsed_arguments = adapters._strict_json_loads(arguments)
                    except adapters.AdapterError as exc:
                        raise _fail("CLI tool call arguments are invalid") from exc
                    if not isinstance(parsed_arguments, Mapping):
                        raise _fail("CLI tool call arguments are invalid")
                    if call_id in tool_calls:
                        raise _fail("CLI emitted duplicate tool call id")
                    if name not in allowed_tools:
                        raise _fail(_TOOL_POLICY_ERROR)
                    tool_calls[call_id] = name
                    if len(tool_calls) > max_tools:
                        raise _fail(_TOOL_LIMIT_ERROR)
            raw_usage = event.get("usage")
            if isinstance(raw_usage, Mapping):
                usage["input_tokens"] = _usage_number(raw_usage.get("input_tokens"))
                usage["output_tokens"] = _usage_number(raw_usage.get("output_tokens"))
                usage["total_tokens"] = _usage_number(raw_usage.get("total_tokens"))
                usage["cost_usd"] = _usage_number(raw_usage.get("cost_usd"))
            continue
        if role == "tool":
            call_id = _nonempty_text(event.get("tool_call_id"), "CLI tool result id", max_length=256)
            if call_id not in tool_calls or call_id in tool_results:
                raise _fail("CLI tool result identity is invalid")
            if not isinstance(event.get("content"), str):
                raise _fail("CLI tool result is invalid")
            tool_results.add(call_id)
            continue
        raise _fail("CLI emitted unsupported stream event")
    if version is None:
        raise _fail("CLI version event missing")
    if expected_version is not None and version != expected_version:
        raise _fail("CLI version drift")
    if session_id is None:
        raise _fail("CLI session resume hint missing")
    if tool_results != set(tool_calls):
        raise _fail("CLI tool result evidence is incomplete")
    if not contents:
        raise _fail("CLI response missing assistant content")
    try:
        payload = adapters._strict_json_loads("".join(contents))
    except adapters.AdapterError as exc:
        raise _fail("CLI response is not JSON") from exc
    responses = adapters._extract_responses(payload, packet)
    return responses, session_id, version, len(tool_calls), usage


def _settings_hash(condition: str, config: Mapping[str, Any]) -> str:
    settings = {
        "legacy_env": KIMI_LEGACY_ENV,
        "legacy_value": "0",
        "profile_tools": [f"mcp__sources__{tool}" for tool in config["tools"]] if condition == "sources" else [],
        "profile_subagents": [],
        "skills_dir": "one-empty-explicit-directory",
        "no_resume_flags": True,
        "output_format": "stream-json",
        "model_flag": "--model",
        "requested_effort": config["effort"],
        "max_output_tokens": config["max_output_tokens"],
        "max_tool_calls": config["max_tool_calls"],
        "effort_env": KIMI_EFFORT_ENV,
        "output_cap_env": KIMI_OUTPUT_ENV,
        "mcp": "controlled-stdio-sources-proxy" if condition == "sources" else "none",
        "telemetry": False,
    }
    return adapters.digest(settings)


def _request_shape_hash(condition: str, config: Mapping[str, Any]) -> str:
    shape = {
        "argv": ["kimi", "-p", "<prompt>", "--agent-file", "<profile>", "--skills-dir", "<empty>", "--model", "<exact-alias>", "--output-format", "stream-json"],
        "condition": condition,
        "profile_tools": [f"mcp__sources__{tool}" for tool in config["tools"]] if condition == "sources" else [],
        "max_tool_calls": config["max_tool_calls"],
        "max_output_tokens": config["max_output_tokens"],
        "proxy_args": ["--url-env", "ZNO_NMT_SOURCES_URL", "--tools", "<exact-allowlist>", "--max-tool-calls", "<positive-cap>"] if condition == "sources" else [],
    }
    return adapters.digest(shape)


def preflight_kimi(
    config: Mapping[str, Any],
    condition: str,
    sources_url: str | None = None,
    *,
    private_env_path: str | os.PathLike[str] | Path | None = None,
) -> dict[str, Any]:
    """Check Kimi installation, native config/catalog, and source capability."""

    options = validate_options(config, private_env_path=private_env_path)
    _validate_condition(options.config, condition, sources_url)
    provisioning = _load_provisioning(options)
    probe = _probe_cli(options.binary, options.config["timeout_seconds"])
    capability: dict[str, Any] = {
        "schema": KIMI_CAPABILITY_SCHEMA,
        "adapter": KIMI_ADAPTER,
        "condition": condition,
        "requested_model": options.config["model"],
        "requested_model_alias": options.config["model"],
        "requested_effort": options.config["effort"],
        "accepted_effort": "unknown",
        "account_identity": "unknown",
        "effective_backend_model": "unknown",
        "tools_sha256": adapters.digest(options.config["tools"]),
        "corpus_id_sha256": adapters.digest(options.config["corpus_id"]) if options.config["corpus_id"] is not None else None,
        "capability": "native-kimi-isolated",
        "cli_version": probe.version,
        "version_observed": probe.version,
        "binary_sha256": probe.binary_sha256,
        "native_config_sha256": provisioning.native_config_sha256,
        "catalog_provider_sha256": provisioning.catalog_provider_sha256,
        "catalog_model_sha256": provisioning.catalog_model_sha256,
        "effort_support_source": "private-native-config-model-alias",
        "supported_efforts": list(provisioning.model.get("support_efforts", [])),
        "max_output_tokens_configured": options.config["max_output_tokens"],
        "max_output_tokens_effective": "unknown",
        "settings_sha256": _settings_hash(condition, options.config),
        "request_shape_sha256": _request_shape_hash(condition, options.config),
    }
    if condition == "sources":
        tools, identity = adapters._mcp_list_tools(str(sources_url), options.config["timeout_seconds"])
        expected = {f"mcp__sources__{tool}" for tool in options.config["tools"]}
        available = {f"mcp__sources__{item['name']}" for item in tools}
        if not expected.issubset(available):
            raise _fail("Sources MCP does not expose configured tools")
        capability["tool_schema_sha256"] = adapters.digest(tools)
        capability["mcp_server_identity_sha256"] = identity
    else:
        capability["tool_schema_sha256"] = adapters.digest([])
        capability["mcp_server_identity_sha256"] = None
    return capability


def run_kimi(
    packet: Mapping[str, Any],
    config: Mapping[str, Any],
    condition: str,
    *,
    sources_url: str | None,
    prompt: str,
    evidence: Callable[[str, Any], None] | None = None,
    private_env_path: str | os.PathLike[str] | Path | None = None,
) -> dict[str, Any]:
    """Run one fresh native Kimi session under an isolated temporary home."""

    options = validate_options(config, private_env_path=private_env_path)
    _validate_condition(options.config, condition, sources_url)
    if not isinstance(prompt, str) or not prompt:
        raise _fail("Kimi prompt must be a nonempty string")
    provisioning = _load_provisioning(options)
    probe = _probe_cli(options.binary, options.config["timeout_seconds"])
    with tempfile.TemporaryDirectory(prefix="kimi-eval-home-") as home_temp, tempfile.TemporaryDirectory(prefix="kimi-eval-cwd-") as cwd_temp:
        home = Path(home_temp)
        cwd = Path(cwd_temp)
        home.chmod(0o700)
        cwd.chmod(0o700)
        _assert_neutral_cwd(cwd)
        _write_safe_config(home, provisioning, options.config["effort"])
        _write_safe_token(home, provisioning)
        profile, skills = _write_profile_and_skills(cwd, condition, options.config["tools"])
        if condition == "sources":
            _write_sources_mcp(home, str(sources_url), options.config["tools"], options.config["max_tool_calls"])
        env = _child_env(home, options.config["max_output_tokens"], options.config["effort"])
        argv = [
            str(probe.binary_path),
            "-p",
            prompt,
            "--agent-file",
            str(profile),
            "--skills-dir",
            str(skills),
            "--model",
            options.config["model"],
            "--output-format",
            "stream-json",
        ]
        if evidence is not None:
            evidence(
                "cli_invocation",
                {
                    "argv": argv,
                    "cwd": str(cwd),
                    "env_keys": sorted(env),
                    "condition": condition,
                    "settings_sha256": _settings_hash(condition, options.config),
                },
            )
        started = time.monotonic()
        completed = _run_kimi_process(
            argv,
            cwd=cwd,
            env=env,
            prompt=prompt,
            timeout=options.config["timeout_seconds"],
            evidence=evidence,
        )
        elapsed = time.monotonic() - started
        if evidence is not None:
            evidence("cli_result", {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr})
        if completed.returncode != 0:
            diagnostics = ((completed.stdout or "") + "\n" + (completed.stderr or "")).casefold()
            if any(marker in diagnostics for marker in ("not logged in", "login", "authentication", "oauth", "no model configured")):
                raise _fail("Kimi CLI authentication unavailable")
            raise _fail("Kimi CLI invocation failed")
        allowed_tools = {f"mcp__sources__{tool}" for tool in options.config["tools"]} if condition == "sources" else set()
        responses, session_id, version, tool_calls, usage = _parse_stream_json(
            completed.stdout or "",
            packet,
            allowed_tools,
            options.config["max_tool_calls"],
            options.config["model"],
            probe.version,
        )
    return {
        "responses": responses,
        "identity": {
            "adapter": KIMI_ADAPTER,
            "harness": KIMI_HARNESS,
            "model": options.config["model"],
            "provider": provisioning.provider_id,
            "account_identity": "unknown",
            "session_id": session_id,
            "requested_model": options.config["model"],
            "requested_model_alias": options.config["model"],
            "effective_model": "unknown",
            "effective_backend_model": "unknown",
            "requested_effort": options.config["effort"],
            "accepted_effort": "unknown",
            "effective_effort": "unknown",
            "cli_version": version,
            "version_observed": version,
            "binary_sha256": probe.binary_sha256,
            "native_config_sha256": provisioning.native_config_sha256,
            "catalog_provider_sha256": provisioning.catalog_provider_sha256,
            "catalog_model_sha256": provisioning.catalog_model_sha256,
            "tool_schema_sha256": adapters.digest(options.config["tools"] if condition == "sources" else []),
            "corpus_id_sha256": adapters.digest(options.config["corpus_id"])
            if options.config["corpus_id"] is not None
            else None,
            "mcp_server_identity_sha256": None,
            "max_output_tokens_configured": options.config["max_output_tokens"],
            "max_output_tokens_effective": "unknown",
            "settings_sha256": _settings_hash(condition, options.config),
            "request_shape_sha256": _request_shape_hash(condition, options.config),
        },
        "metrics": {
            "elapsed_seconds": elapsed,
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
            "cost_usd": usage["cost_usd"],
            "tool_calls": tool_calls,
        },
    }


# Short aliases make the adapter easy for the parent runner to integrate while
# keeping the explicit provider name available for callers that prefer it.
preflight = preflight_kimi
run = run_kimi


__all__ = [
    "KIMI_ADAPTER",
    "KIMI_PROVIDER",
    "KimiAdapterError",
    "NativeKimiOptions",
    "preflight",
    "preflight_kimi",
    "run",
    "run_kimi",
    "validate_config",
    "validate_kimi_config",
    "validate_options",
]
