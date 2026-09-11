"""Owned, no-prompt AGY status in a fresh access-token-only native home."""
from __future__ import annotations

import os
import pty
import re
import selectors
import signal
import ssl
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from probe_common import MAX_BYTES, NoRedirect, Process, canonical, fail, file_hash, parse, text, timestamp, utcnow

AUTH_PATH = Path(".gemini/antigravity-cli/antigravity-oauth-token")
METHODS = {"RetrieveUserQuotaSummary", "GetUserStatus"}
DEADLINE_SECONDS = 55
LSOF = "/usr/sbin/lsof"


def environment(home):
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    env.update(HOME=str(home), TERM="xterm-256color", XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_STATE_HOME=str(home / ".local/state"))
    return env


def provision(config, bearer, home):
    source_home = Path(text(config.get("native_home")))
    if not source_home.is_absolute():
        fail("auth_provisioning_invalid")
    source = source_home / AUTH_PATH
    try:
        for item in (source_home, source_home / ".gemini", source.parent, source):
            info = item.lstat()
            if item.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                fail("auth_provisioning_invalid")
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
            fail("auth_provisioning_invalid")
        original_hash = file_hash(source)
        value = parse(source.read_bytes())
    except OSError:
        fail("auth_provisioning_unavailable")
    if (not isinstance(value, dict) or not {"auth_method", "token"} <= value.keys()
            or set(value) - {"auth_method", "token", "id_token"} or value["auth_method"] != "consumer"
            or ("id_token" in value and not isinstance(value["id_token"], str))):
        fail("auth_provisioning_invalid")
    token = value["token"]
    if (not isinstance(token, dict) or set(token) - {"access_token", "expiry", "refresh_token", "token_type"}
            or token.get("access_token") != bearer or token.get("token_type") != "Bearer"):
        fail("auth_provisioning_invalid")
    if timestamp(token.get("expiry")) <= datetime.now(UTC) + timedelta(seconds=DEADLINE_SECONDS + 20):
        fail("auth_expiry_insufficient")
    copied = {"auth_method": "consumer", "token": {key: token[key] for key in ("access_token", "expiry", "token_type")}}
    target = home / AUTH_PATH
    target.parent.mkdir(parents=True, mode=0o700)
    target.write_bytes(canonical(copied))
    target.chmod(0o600)
    return source, original_hash, target, file_hash(target)


class OwnedNative:
    def __init__(self, binary, home, workspace, deadline):
        self.deadline, self.env, self.total = deadline, environment(home), 0
        self.master, slave = pty.openpty()
        self.selector = selectors.DefaultSelector()
        try:
            self.process = subprocess.Popen([binary], cwd=workspace, env=self.env, stdin=slave,
                stdout=slave, stderr=slave, start_new_session=True)
            self.selector.register(self.master, selectors.EVENT_READ)
        except OSError:
            os.close(self.master)
            self.selector.close()
            fail("native_spawn_failed")
        finally:
            os.close(slave)

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            fail("native_timeout")
        return remaining

    def owned(self):
        try:
            if self.process.poll() is not None or os.getpgid(self.process.pid) != self.process.pid:
                fail("native_ownership_lost")
        except ProcessLookupError:
            fail("native_ownership_lost")

    def drain(self, wait=0):
        self.remaining()
        for key, _ in self.selector.select(min(wait, self.remaining())):
            try:
                chunk = os.read(key.fd, 65536)
            except OSError:
                self.selector.unregister(key.fd)
                continue
            if not chunk:
                self.selector.unregister(key.fd)
            self.total += len(chunk)
            if self.total > MAX_BYTES:
                fail("native_output_overflow")

    def ports(self):
        self.owned()
        listing = Process([LSOF, "-nP", "-a", "-p", str(self.process.pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
                          env=self.env, timeout=min(3, self.remaining()))
        try:
            listing.process.stdin.close()
            raw = b"".join(listing.chunks())
        finally:
            listing.close()
        self.owned()
        return {(match.group(1), int(match.group(2))) for line in raw.decode("ascii", errors="strict").splitlines()
                if (match := re.fullmatch(r"n(127\.0\.0\.1|\[::1\]):([0-9]{1,5})", line))
                and 0 < int(match.group(2)) < 65536}

    def rpc(self, endpoint, method, body):
        if method not in METHODS or endpoint not in self.ports():
            fail("native_endpoint_rejected")
        self.owned()
        self.drain()
        host, port = endpoint
        url = f"https://{host}:{port}/exa.language_server_pb.LanguageServerService/{method}"
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Self-signed owned native loopback only.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                            urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(url, data=canonical(body), method="POST",
            headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1"})
        with opener.open(request, timeout=min(5, self.remaining())) as response:
            if response.status != 200 or response.geturl() != url or response.headers.get("Age") not in (None, "0"):
                fail("native_response_rejected")
            raw = bytearray()
            while True:
                self.remaining()
                self.owned()
                self.drain()
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(raw)))
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    fail("response_too_large")
                if not chunk:
                    break
        self.owned()
        return parse(bytes(raw))

    def close(self):
        try:
            if self.process.poll() is None:
                self.owned()
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.process.wait(timeout=5)
        finally:
            self.selector.close()
            os.close(self.master)


def collect(config, bearer, identity_reader, requested_at, diagnostics=None):
    if timestamp(requested_at) > timestamp(utcnow()):
        fail("status_not_fresh")
    runtime_hash = file_hash(config["binary"])
    deadline = time.monotonic() + DEADLINE_SECONDS
    with tempfile.TemporaryDirectory(prefix="admission-agy-") as temp:
        home = Path(temp) / "home"
        home.mkdir(mode=0o700)
        source, source_hash, target, target_hash = provision(config, bearer, home)
        identity = identity_reader(bearer)
        if (not isinstance(identity, dict) or identity.get("email_verified") is not True
                or not identity.get("sub") or not identity.get("email")):
            fail("subscription_identity_mismatch")
        workspace = Path(temp) / "workspace"
        workspace.mkdir(mode=0o700)
        setup = Process(["/usr/bin/git", "init", "-q", str(workspace)], env=environment(home),
                        timeout=min(3, max(0.01, deadline - time.monotonic())))
        try:
            setup.process.stdin.close()
            b"".join(setup.chunks())
            if setup.process.wait(timeout=1):
                fail("native_workspace_failed")
        finally:
            setup.close()
        child = OwnedNative(config["binary"], home, workspace, deadline)
        try:
            if diagnostics is not None:
                diagnostics.append({"stage": "owned_native", "state": "started"})
            backoff = {}
            while True:
                child.remaining()
                child.owned()
                child.drain(0.15)
                for endpoint in child.ports():
                    if time.monotonic() < backoff.get(endpoint, 0):
                        continue
                    backoff[endpoint] = time.monotonic() + 2
                    try:
                        quota = child.rpc(endpoint, "RetrieveUserQuotaSummary", {"forceRefresh": True})
                        status = child.rpc(endpoint, "GetUserStatus", {"metadata": {
                            "ideName": "antigravity", "extensionName": "antigravity", "ideVersion": "unknown", "locale": "en"}})
                    except (urllib.error.URLError, TimeoutError, OSError):
                        continue
                    if diagnostics is not None:
                        diagnostics.append({"stage": "owned_native", "state": "completed"})
                    return identity, status, quota, utcnow()
        finally:
            child.close()
            if file_hash(source) != source_hash or file_hash(target) != target_hash:
                fail("auth_provisioning_changed")
            if file_hash(config["binary"]) != runtime_hash:
                fail("runtime_identity_mismatch")
