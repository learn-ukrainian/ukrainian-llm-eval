"""Synthetic operator staging for credential-free native contract tests."""
import json
import time

import pytest

from ukrainian_llm_eval import adapters, native_cursor


@pytest.fixture(autouse=True)
def staged_native_auth(tmp_path, monkeypatch):
    root = tmp_path / "synthetic-staging"
    root.mkdir(mode=0o700)
    for name, value in ((".credentials.json", {"claudeAiOauth": {
            "accessToken": "synthetic-access", "refreshToken": "synthetic-refresh-never-copy",
            "expiresAt": (time.time() + 10000) * 1000, "scopes": ["user:inference"]}}),
                        ("auth.json", {"accessToken": "synthetic-access", "refreshToken": "synthetic-refresh"})):
        path = root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    monkeypatch.setenv("UKRAINIAN_LLM_EVAL_CLAUDE_PROVISIONING_DIR", str(root))
    monkeypatch.setenv("UKRAINIAN_LLM_EVAL_CURSOR_PROVISIONING_DIR", str(root))
    # These legacy tests use synthetic binaries, not installed public bundles.
    monkeypatch.setattr(native_cursor, "_auth_contract", lambda _binary: None)
    monkeypatch.setattr(adapters, "_claude_auth_contract", lambda _binary: None)
    yield root


def emit_agy_capture(root, session="00000000-0000-0000-0000-000000000001"):
    log = root / "native-log.txt"
    log.write_text("Created conversation " + session + "\n")
    log.chmod(0o600)
    transcript = root / "home/.gemini/antigravity-cli/brain" / session / ".system_generated/logs/transcript.jsonl"
    transcript.parent.mkdir(mode=0o700, parents=True)
    for directory in transcript.parents:
        if directory == root:
            break
        directory.chmod(0o700)
    transcript.write_text('{"type":"USER_INPUT"}\n')
    transcript.chmod(0o600)
    return session


@pytest.fixture(autouse=True)
def synthetic_catalog(monkeypatch):
    from ukrainian_llm_eval import mcp_proxy
    names = adapters.SMOKE_TOOLS
    catalog = [{"name": name, "inputSchema": {"type": "object"}, "description": "Synthetic reference " + name}
               for name in names]
    monkeypatch.setattr(mcp_proxy, "SMOKE_CATALOG_SHA256", adapters.digest(catalog))
