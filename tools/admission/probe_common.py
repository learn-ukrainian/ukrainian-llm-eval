"""Standard-library trust boundary for native admission status collectors."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import signal
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 2_000_000


class ProbeError(ValueError):
    """Only constant, non-secret classifications cross this boundary."""


def fail(reason):
    raise ProbeError(reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("duplicate_json_key")
        result[key] = value
    return result


def parse(raw):
    if len(raw) > MAX_BYTES:
        fail("response_too_large")
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: fail("invalid_json"))
    except (ValueError, UnicodeError):
        fail("invalid_json")


def text(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 4096:
        fail("missing_identity")
    if any(ord(char) < 32 for char in value):
        fail("invalid_identity")
    return value


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        fail("invalid_capacity")
    return value


def timestamp(value):
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            fail("invalid_timestamp")
        return date.astimezone(UTC)
    except (ValueError, TypeError):
        fail("invalid_timestamp")


def utcnow():
    return datetime.now(UTC).isoformat()


def validate_request(request, now=None):
    fields = {"schema", "nonce", "requested_at", "route_sha256", "model", "effort", "condition",
              "composite_sha256", "requirements", "request_sha256"}
    if not isinstance(request, dict) or set(request) != fields:
        fail("invalid_request")
    if request["schema"] != "ukrainian-llm-eval.admission-request.v1":
        fail("invalid_request")
    if not isinstance(request["nonce"], str) or re.fullmatch(r"[0-9a-f]{32}", request["nonce"]) is None:
        fail("invalid_nonce")
    if request["request_sha256"] != digest({k: v for k, v in request.items() if k != "request_sha256"}):
        fail("request_hash_mismatch")
    for key in ("route_sha256", "composite_sha256"):
        if not isinstance(request[key], str) or re.fullmatch(r"[0-9a-f]{64}", request[key]) is None:
            fail("invalid_request")
    age = ((now or datetime.now(UTC)) - timestamp(request["requested_at"])).total_seconds()
    if not 0 <= age <= 300:
        fail("stale_request")
    requirements = request["requirements"]
    keys = {"input_utf8_bytes", "max_total_input_tokens", "max_total_output_tokens", "max_output_tokens",
            "max_tool_calls", "timeout_seconds", "tool_policy_sha256"}
    if not isinstance(requirements, dict) or set(requirements) != keys:
        fail("invalid_requirements")
    for key in keys - {"tool_policy_sha256"}:
        integer(requirements[key])
    if request["effort"] not in {"low", "medium", "high"}:
        fail("unsupported_effort")
    text(request["model"])
    return request


def file_hash(path):
    try:
        target = Path(path)
        if not target.is_file() or target.is_symlink():
            fail("runtime_identity_mismatch")
        with target.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        fail("runtime_unavailable")


def verified_runtime(config):
    files = config["runtime_files"]
    if not isinstance(files, list) or not files:
        fail("runtime_evidence_missing")
    expected = {}
    for item in files:
        if set(item) != {"path", "byte_sha256"} or not os.path.isabs(item["path"]):
            fail("runtime_evidence_invalid")
        if item["path"] in expected or file_hash(item["path"]) != item["byte_sha256"]:
            fail("runtime_identity_mismatch")
        expected[item["path"]] = item["byte_sha256"]
    if config["binary"] not in expected:
        fail("runtime_evidence_missing")
    return digest(files)


def child_env():
    # Never propagate API keys, endpoint overrides, proxies, or code-loading env.
    return {key: value for key, value in os.environ.items()
            if key in {"PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}}


class Process:
    """Bound total time/output, drain both pipes, and never surface raw errors."""
    def __init__(self, argv, *, env, timeout=30, diagnostics=None):
        self.diagnostics = diagnostics
        try:
            self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, env=env, start_new_session=True)
        except OSError:
            fail("native_spawn_failed")
        self.deadline = time.monotonic() + timeout
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        self.buffer = b""
        self.total = 0

    def diagnostic(self, direction, event):
        if self.diagnostics is None or len(self.diagnostics) >= 200:
            return
        allowed = {"initialize", "initialized", "account/read", "account/rateLimits/read", "model/list",
                   "account/chatgptAuthTokens/refresh", "attestation/generate", "account/updated",
                   "account/rateLimits/updated"}
        method = event.get("method")
        code = (event.get("error") or {}).get("code") if isinstance(event.get("error"), dict) else None
        self.diagnostics.append({"direction": direction,
            "method": method if method in allowed else ("unrecognized" if method else None),
            "id": event.get("id") if type(event.get("id")) is int else None,
            "error_code": code if type(code) is int else None,
            "has_result": "result" in event})

    def send(self, message):
        self.diagnostic("sent", message)
        try:
            self.process.stdin.write(canonical(message) + b"\n")
            self.process.stdin.flush()
        except (OSError, BrokenPipeError):
            fail("native_protocol_failed")

    def chunks(self):
        while self.selector.get_map():
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                fail("native_timeout")
            for key, _ in self.selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    continue
                self.total += len(chunk)
                if self.total > MAX_BYTES:
                    fail("native_output_overflow")
                if key.data == "stdout":
                    yield chunk

    def response(self, request_id):
        for chunk in self.chunks():
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                event = parse(line)
                if not isinstance(event, dict):
                    fail("native_protocol_failed")
                self.diagnostic("received", event)
                if "id" in event:
                    if event.get("id") != request_id or "error" in event or "result" not in event:
                        fail("native_protocol_failed")
                    return event["result"]
        fail("native_response_missing")

    def close(self):
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=5)
        self.selector.close()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


def native_json(argv, env):
    process = Process(argv, env=env)
    try:
        process.process.stdin.close()
        raw = b"".join(process.chunks())
        if process.process.wait(timeout=1):
            fail("native_status_failed")
        return parse(raw)
    finally:
        process.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fail("provider_redirect_rejected")


ENDPOINTS = {
    "https://api.anthropic.com/api/oauth/usage": "GET",
    "https://api.anthropic.com/api/oauth/profile": "GET",
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": "POST",
    "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels": "POST",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota": "POST",
    "https://openidconnect.googleapis.com/v1/userinfo": "GET",
}


def provider_json(url, token, *, body=None, headers=None):
    method = "GET" if body is None else "POST"
    if ENDPOINTS.get(url) != method:
        fail("provider_endpoint_rejected")
    text(token)
    request = urllib.request.Request(url, data=canonical(body) if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                 "Content-Type": "application/json", "Cache-Control": "no-cache", **(headers or {})}, method=method)
    # Environment proxies can redirect bearer credentials; never inherit them.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200 or response.geturl() != url:
                fail("provider_response_rejected")
            if response.headers.get("Age") not in (None, "0"):
                fail("cached_provider_response")
            return parse(response.read(MAX_BYTES + 1))
    except urllib.error.HTTPError as exc:
        if type(exc.code) is int and 100 <= exc.code <= 599:
            fail("provider_http_" + str(exc.code))
        fail("provider_status_unavailable")
    except (urllib.error.URLError, OSError):
        fail("provider_status_unavailable")


def available_percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        fail("quota_unknown")
    if not 0 <= value < 100:
        fail("quota_exhausted")
