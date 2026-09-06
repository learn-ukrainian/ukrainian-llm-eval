"""Restricted native OpenCode execution using its OpenRouter provider.

The CLI constructs prompts and executes reference tools. An authenticated local
gate forwards exact checked requests, accounts for every provider response, and
rejects retries and route/tool drift. No existing OpenCode home is imported.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import adapters
from .opencode_gateway import OpenCodeGateway

_ENV = frozenset({"PATH", "USER", "LOGNAME", "TMPDIR", "SHELL", "TERM", "LANG", "LC_ALL",
                  "SSL_CERT_FILE", "SSL_CERT_DIR"})


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config, Mapping) or config.get("adapter") != "opencode":
        raise adapters.AdapterError("wrong native OpenCode adapter")
    checked = dict(config)
    binary = checked.pop("opencode_bin", "opencode")
    if not isinstance(binary, str) or not binary.strip():
        raise adapters.AdapterError("OpenCode binary invalid")
    if "http_response_format" in checked:
        raise adapters.AdapterError("OpenCode uses native structured output")
    checked["adapter"] = "chat-http"
    checked["http_response_format"] = "json_object"
    checked = adapters.validate_config(checked)
    if checked["provider"] != "openrouter" or not isinstance(checked.get("openrouter"), dict):
        raise adapters.AdapterError("OpenCode requires explicit OpenRouter routing")
    if checked["model"] != "google/gemma-4-31b-it" or checked["effort"] is not None:
        raise adapters.AdapterError("OpenCode route supports Gemma reasoning off/on without named effort")
    if not checked.get("key_env"):
        raise adapters.AdapterError("OpenCode provider key environment is required")
    checked.pop("http_response_format")
    checked.update(adapter="opencode", opencode_bin=binary)
    return checked


def child_env(home: Path, config_path: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _ENV}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_STATE_HOME=str(home / ".local/state"), OPENCODE_CONFIG=str(config_path),
               OPENCODE_DISABLE_PROJECT_CONFIG="true", OPENCODE_DISABLE_CLAUDE_CODE="true",
               OPENCODE_DISABLE_AUTOUPDATE="true")
    return env


def native_config(config: Mapping[str, Any], condition: str, gateway: OpenCodeGateway) -> dict[str, Any]:
    model = "openrouter/" + config["model"]
    permissions = {"*": "deny", "StructuredOutput": "allow", **{name: "allow" for name in sorted(gateway.allowed)}}
    routing = config["openrouter"]
    provider = {"only": [routing["provider_endpoint"]], "allow_fallbacks": False, "require_parameters": True}
    if "max_price" in routing:
        provider["max_price"] = dict(routing["max_price"])
    result: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
        "enabled_providers": ["openrouter"], "model": model, "small_model": model,
        "permission": permissions, "compaction": {"auto": False, "prune": False},
        "agent": {"eval": {"description": "Ukrainian evaluator", "mode": "primary",
                           "prompt": "Answer the supplied evaluation packet and obey its response contract.",
                           "permission": permissions, "steps": config["max_tool_calls"] + 2},
                  "title": {"disable": True}, "summary": {"disable": True}},
        "provider": {"openrouter": {"options": {"apiKey": gateway.token,
                                                "baseURL": gateway.url + "/api/v1"},
                                    "models": {config["model"]: {
                                        "limit": {"context": 256000, "output": config["max_output_tokens"]},
                                        "options": {"reasoning": {"enabled": routing["reasoning_enabled"]},
                                                    "provider": provider}}}}},
    }
    if condition == "sources":
        result["mcp"] = {"sources": {"type": "remote", "url": gateway.url + "/mcp", "oauth": False,
                                     "headers": {"Authorization": "Bearer " + gateway.token}}}
    return result


def _binary(config: Mapping[str, Any]) -> tuple[str, str, str]:
    binary = shutil.which(str(config["opencode_bin"]))
    if binary is None:
        raise adapters.AdapterError("OpenCode binary unavailable")
    with tempfile.TemporaryDirectory(prefix="opencode-probe-") as temp:
        home = Path(temp)
        env = child_env(home, home / "absent-config.json")
        result = subprocess.run([binary, "--version"], env=env, cwd=home, capture_output=True,
                                text=True, timeout=min(15, config["timeout_seconds"]), check=False)
    version = result.stdout.strip()
    if result.returncode or not version or len(version) > 160:
        raise adapters.AdapterError("OpenCode version unavailable")
    return binary, version, hashlib.sha256(Path(binary).resolve().read_bytes()).hexdigest()


def _endpoint(config: Mapping[str, Any]) -> tuple[str, str]:
    endpoint = os.environ.get(config["endpoint_env"], "")
    key = os.environ.get(config["key_env"], "")
    parsed = urllib.parse.urlsplit(endpoint)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (not key or not parsed.netloc or parsed.username or parsed.password or parsed.fragment
            or not (parsed.scheme == "https" or (parsed.scheme == "http" and local))):
        raise adapters.AdapterError("OpenCode provider endpoint or key unavailable")
    return endpoint, key


def preflight(config: Mapping[str, Any], condition: str, sources_url: str | None = None) -> dict[str, Any]:
    checked = validate_config(config)
    adapters._condition_policy(checked, condition, sources_url)
    _endpoint(checked)
    _, version, binary_hash = _binary(checked)
    tools, identity = adapters._mcp_list_tools(str(sources_url), checked["timeout_seconds"]) if condition == "sources" else ([], None)
    if condition == "sources" and not set(checked["tools"]) <= {tool["name"] for tool in tools}:
        raise adapters.AdapterError("Sources MCP does not expose configured tools")
    return {"schema": "zno-nmt.capability.v1", "adapter": "opencode", "condition": condition,
            "requested_model": checked["model"], "requested_effort": None,
            "tools_sha256": adapters.digest(checked["tools"]), "tool_schema_sha256": adapters.digest(tools),
            "corpus_id_sha256": adapters.digest(checked["corpus_id"]) if checked["corpus_id"] else None,
            "mcp_server_identity_sha256": identity, "capability": "native-opencode-guarded",
            "cli_version": version, "binary_sha256": binary_hash}


def parse_messages(messages: Any, packet: Mapping[str, Any], allowed: set[str]) -> tuple[dict[str, Any], str, int]:
    if not isinstance(messages, list) or not messages:
        raise adapters.AdapterError("OpenCode message evidence missing")
    sessions: set[str] = set()
    calls = 0
    final = None
    for message in messages:
        info = message.get("info", {})
        session = info.get("sessionID")
        if not isinstance(session, str) or not session or info.get("error"):
            raise adapters.AdapterError("OpenCode native message error")
        sessions.add(session)
        if info.get("role") != "assistant":
            continue
        if final is not None:
            raise adapters.AdapterError("OpenCode output after final answer")
        terminal = []
        for part in message.get("parts", []):
            if part.get("type") != "tool":
                continue
            name, state = part.get("tool"), part.get("state", {})
            if state.get("status") != "completed":
                raise adapters.AdapterError("OpenCode native tool failed")
            if name == "StructuredOutput":
                terminal.append(state.get("input"))
            elif name in allowed:
                calls += 1
            else:
                raise adapters.AdapterError("tool_policy_error")
        if "structured" in info:
            if terminal != [info["structured"]]:
                raise adapters.AdapterError("OpenCode structured evidence mismatch")
            final = adapters._extract_responses(info["structured"], packet)
        elif terminal:
            raise adapters.AdapterError("OpenCode structured output unavailable")
    if len(sessions) != 1 or final is None:
        raise adapters.AdapterError("OpenCode final response unavailable")
    return final, next(iter(sessions)), calls


def _run_native_server(binary: str, *, cwd: Path, env: dict[str, str], prompt: str,
                       config: Mapping[str, Any], packet: Mapping[str, Any],
                       deadline: float, evidence: Callable[[str, Any], None] | None,
                       gateway: OpenCodeGateway) -> Any:
    """Use OpenCode's native schema API, with an isolated authenticated server."""
    password = secrets.token_urlsafe(32)
    env = {**env, "OPENCODE_SERVER_PASSWORD": password}
    argv = [binary, "serve", "--pure", "--hostname", "127.0.0.1", "--port", "0"]
    if evidence is not None:
        evidence("cli_invocation", {"argv": argv, "env_keys": sorted(env),
                                    "config_sha256": adapters.digest(config)})
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=stderr, start_new_session=True)
        try:
            assert process.stdout is not None
            startup = b""
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while b"\n" not in startup:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise adapters.AdapterError("OpenCode startup timeout")
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk or len(startup) + len(chunk) > 8192:
                        raise adapters.AdapterError("OpenCode startup failed")
                    startup += chunk
            match = re.search(rb"http://127\.0\.0\.1:(\d+)", startup)
            if match is None:
                raise adapters.AdapterError("OpenCode server address unavailable")
            url = "http://127.0.0.1:" + match[1].decode()
            authorization = "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()
            opener = urllib.request.build_opener(adapters._RejectRedirects())

            def request(path: str, body: Any = None) -> Any:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise adapters.AdapterError("OpenCode total timeout")
                req = urllib.request.Request(url + path,
                    data=adapters.canonical(body).encode() if body is not None else None,
                    headers={"Authorization": authorization, **({"Content-Type": "application/json"} if body is not None else {})})
                try:
                    with opener.open(req, timeout=remaining) as response:  # nosec B310 -- fixed authenticated loopback
                        raw = response.read(2_000_001)
                except urllib.error.HTTPError as exc:
                    if evidence is not None:
                        evidence("opencode_api_error", {"path": path, "status": exc.code, "body": exc.read(8192).decode(errors="replace")})
                    raise adapters.AdapterError("OpenCode native API rejected request") from exc
                if len(raw) > 2_000_000:
                    raise adapters.AdapterError("OpenCode native evidence exceeds limit")
                return adapters._strict_json_loads(raw.decode())

            session = request("/session", {"title": "Ukrainian evaluation"})
            session_id = session.get("id")
            if not isinstance(session_id, str) or not re.fullmatch(r"ses_[A-Za-z0-9]+", session_id):
                raise adapters.AdapterError("OpenCode session identity invalid")
            answer = request("/session/" + session_id + "/message", {
                "model": {"providerID": "openrouter", "modelID": config["model"]}, "agent": "eval",
                "parts": [{"type": "text", "text": prompt}],
                "format": {"type": "json_schema", "schema": adapters.response_schema(packet), "retryCount": 0}})
            if evidence is not None:
                evidence("opencode_native_answer", answer)
            messages = request("/session/" + session_id + "/message?limit=" + str(len(gateway.requests)))
            if not isinstance(messages, list) or len(messages) != len(gateway.requests) or any(
                message.get("info", {}).get("role") != "assistant" for message in messages
            ):
                raise adapters.AdapterError("OpenCode assistant evidence incomplete")
            if evidence is not None:
                evidence("opencode_native_messages", messages)
            return messages
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


def run_opencode(packet: Mapping[str, Any], config: Mapping[str, Any], condition: str, *,
                 sources_url: str | None, prompt: str, evidence: Callable[[str, Any], None] | None = None,
                 request_budget: Any = None) -> dict[str, Any]:
    checked = validate_config(config)
    adapters._condition_policy(checked, condition, sources_url)
    endpoint, key = _endpoint(checked)
    # Live paid access must never run without an accounting controller. Local
    # fixtures use no real credentials and are exempt from paid accounting.
    if request_budget is None and urllib.parse.urlsplit(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise adapters.AdapterError("OpenCode live execution requires request-level budget")
    binary, version, binary_hash = _binary(checked)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="opencode-eval-") as temp:
        root = Path(temp)
        root.chmod(0o700)
        home, workspace = root / "home", root / "workspace"
        home.mkdir(mode=0o700)
        workspace.mkdir(mode=0o700)
        with OpenCodeGateway(checked, condition, endpoint=endpoint, key=key, sources_url=sources_url,
                             evidence=evidence, request_budget=request_budget,
                             deadline=started + checked["timeout_seconds"], packet=packet) as gateway:
            config_path = home / "opencode.json"
            config_path.write_text(adapters.canonical(native_config(checked, condition, gateway)), encoding="utf-8")
            config_path.chmod(0o600)
            env = child_env(home, config_path)
            messages = _run_native_server(binary, cwd=workspace, env=env, prompt=prompt,
                                          config=checked, packet=packet,
                                          deadline=started + checked["timeout_seconds"], evidence=evidence, gateway=gateway)
            if gateway.error is not None:
                if isinstance(gateway.error, adapters.AdapterError):
                    raise gateway.error
                raise adapters.AdapterError("OpenCode transport or reference failure") from gateway.error
            responses, session, calls = parse_messages(messages, packet, gateway.allowed)
            if calls != gateway.tool_calls or any(gateway.pending_calls.values()) or not gateway.requests:
                raise adapters.AdapterError("OpenCode native tool evidence mismatch")
            if adapters._extract_responses(gateway.structured_output, packet) != responses:
                raise adapters.AdapterError("OpenCode final response differs from provider")
            return {"responses": responses, "identity": {
                "adapter": "opencode", "harness": "opencode-cli", "provider": "openrouter", "model": checked["model"],
                "session_id": session, "requested_model": checked["model"], "effective_model": checked["model"],
                "requested_effort": None, "effective_effort": "unknown", "cli_version": version,
                "binary_sha256": binary_hash, "tool_schema_sha256": adapters.digest(sorted(gateway.allowed)),
                "corpus_id_sha256": adapters.digest(checked["corpus_id"]) if checked["corpus_id"] else None,
                "max_output_tokens_configured": checked["max_output_tokens"],
                "max_output_tokens_effective": checked["max_output_tokens"],
                "requested_reasoning_enabled": checked["openrouter"]["reasoning_enabled"],
                "effective_reasoning_enabled": "unknown", "effective_provider": checked["openrouter"]["expected_provider_name"],
            }, "metrics": {"elapsed_seconds": time.monotonic() - started, **gateway.usage,
                           "cost_usd": float(gateway.cost), "tool_calls": calls}}
