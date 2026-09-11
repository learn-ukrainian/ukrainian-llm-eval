"""Deterministic cleanup and isolated provisioning regressions; no provider calls."""
import importlib
import io
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

PROBES = Path(__file__).resolve().parents[2] / "tools" / "admission"
sys.path.insert(0, str(PROBES))
common = importlib.import_module("probe_common")
agy = importlib.import_module("native_agy_status")


def test_completed_helper_is_reaped_without_signalling(monkeypatch):
    process = object.__new__(common.Process)
    waits = []
    process.process = SimpleNamespace(pid=123, wait=lambda **kwargs: waits.append(kwargs),
                                     stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
    process.selector = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(common.os, "killpg", lambda *_: pytest.fail("completed helper must not be signalled"))
    process.close()
    assert waits == [{"timeout": 0.2}]
    assert process.process.stdout.closed


def test_live_helper_is_signalled_then_reaped(monkeypatch):
    process = object.__new__(common.Process)
    waits, signals = [], []
    def wait(**kwargs):
        waits.append(kwargs)
        if len(waits) == 1:
            raise subprocess.TimeoutExpired("synthetic", 0.2)
    process.process = SimpleNamespace(pid=123, wait=wait, stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
    process.selector = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(common.os, "killpg", lambda *args: signals.append(args))
    process.close()
    assert len(signals) == 1 and signals[0][0] == 123
    assert waits[-1] == {"timeout": 5}


def test_cleanup_permission_failure_cannot_hide_live_process(monkeypatch):
    process = object.__new__(common.Process)
    def wait(**kwargs):
        raise subprocess.TimeoutExpired("synthetic", kwargs["timeout"])
    def denied(*args):
        raise PermissionError("synthetic")
    process.process = SimpleNamespace(pid=123, wait=wait, stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
    process.selector = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(common.os, "killpg", denied)
    with pytest.raises(common.ProbeError, match="native_cleanup_failed"):
        process.close()
    assert process.process.stdout.closed


@pytest.mark.skipif(not Path("/usr/sbin/lsof").is_file(), reason="installed macOS lsof unavailable")
def test_installed_lsof_status_helper_reaps():
    process = common.Process(["/usr/sbin/lsof", "-nP", "-a", "-p", str(os.getpid()),
                              "-iTCP", "-sTCP:LISTEN", "-Fn"], env=common.child_env(), timeout=3)
    try:
        process.process.stdin.close()
        b"".join(process.chunks())
    finally:
        process.close()
    assert process.process.poll() is not None


def test_optional_identity_token_is_never_copied(tmp_path):
    source_home = tmp_path / "source"
    source_home.mkdir(mode=0o700)
    (source_home / ".gemini").mkdir(mode=0o700)
    source = source_home / agy.AUTH_PATH
    source.parent.mkdir(mode=0o700)
    source.write_bytes(common.canonical({"auth_method": "consumer", "id_token": "synthetic-id", "token": {
        "access_token": "synthetic-access", "expiry": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "refresh_token": "synthetic-refresh", "token_type": "Bearer"}}))
    source.chmod(0o600)
    _, _, target, _ = agy.provision({"native_home": str(source_home)}, "synthetic-access", tmp_path / "isolated")
    copied = common.parse(target.read_bytes())
    assert "id_token" not in copied and "refresh_token" not in copied["token"]
    assert common.parse(source.read_bytes())["id_token"] == "synthetic-id"
