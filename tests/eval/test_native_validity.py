"""Credential-free isolation, provenance and failure-wall regressions (#68/#65)."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from native_validity_fixtures import emit_agy_capture
from test_native_agy import call_receipts, config, events, hook_receipts, packet, serialize
from test_native_cursor import _packet, _stream_events

from ukrainian_llm_eval import adapters, agy_hook, native_agy, native_codex, native_cursor, runner
from ukrainian_llm_eval import mcp_proxy as proxy
from ukrainian_llm_eval.codex_reference_bridge import ReferenceBridge

CATALOG_FILE = Path(__file__).parent / "fixtures/sources-tool-schemas.json"


def write_private(path, value):
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    path.chmod(0o600)
    return path


def staging(tmp_path, *, claude=None, cursor=None):
    root = tmp_path / "staging"
    root.mkdir(mode=0o700)
    if claude is not None:
        write_private(root / ".credentials.json", claude)
    if cursor is not None:
        write_private(root / "auth.json", cursor)
    return root


def oauth(**extra):
    return {"accessToken": "synthetic-access", "refreshToken": "never-copy-refresh",
            "expiresAt": (time.time() + 10000) * 1000, "scopes": ["user:inference"],
            "subscriptionType": "test", "rateLimitTier": None, **extra}


@pytest.mark.parametrize("parallel", [False, True])
def test_attempts_are_distinct_and_exclude_ambient_state(tmp_path, monkeypatch, parallel):
    sentinels = {name: "AMBIENT_SENTINEL" for name in (
        "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
        "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CURSOR_CONFIG_DIR", "ANTHROPIC_API_KEY", "CURSOR_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NODE_COMPILE_CACHE",
        "AGENT_CLI_CREDENTIAL_STORE", "OPENAI_BASE_URL", "PYTHONPATH")}
    for key, value in sentinels.items():
        monkeypatch.setenv(key, value)
    homes = []
    def invoke(_index):
        with adapters._native_attempt() as attempt:
            homes.append(attempt["root"])
            env = attempt["env"]
            assert "AMBIENT_SENTINEL" not in env.values()
            assert not set(sentinels) - {"HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "CODEX_HOME"} & env.keys()
            for key in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "CODEX_HOME"):
                path = Path(env[key])
                assert path.is_relative_to(attempt["root"])
                assert stat.S_IMODE(path.stat().st_mode) == 0o700
            return attempt["id"]
    if parallel:
        with ThreadPoolExecutor(max_workers=2) as pool:
            identities = list(pool.map(invoke, [0, 1]))
    else:
        identities = list(map(invoke, [0, 1]))
    assert len(set(identities)) == len(set(homes)) == 2
    assert all(not path.exists() for path in homes)


@pytest.mark.parametrize("runtime", ["sol", "luna", "flash", "grok", "sonnet", "opus"])
def test_all_six_probe_env_and_cwd_are_the_candidate_boundary(monkeypatch, tmp_path, runtime):
    seen = []
    def probe(argv, **kwargs):
        seen.append((kwargs["cwd"], dict(kwargs["env"])))
        if runtime in {"sol", "luna"}:
            text = " ".join(native_codex._REQUIRED_HELP_FLAGS) if "--help" in argv else "fixture"
        elif runtime == "flash":
            text = "--agent --model --effort --json-schema --input-format --output-format --disable-slash-commands --print-timeout --log-file"
        elif runtime == "grok":
            text = "Logged in" if "status" in argv else " ".join(native_cursor._REQUIRED_HELP_FLAGS)
        else:
            text = " ".join(adapters._CLAUDE_FLAGS) + " --allowedTools"
        return subprocess.CompletedProcess(argv, 0, text, "")
    monkeypatch.setattr(subprocess, "run", probe)
    binary = write_private(tmp_path / "binary", b"fixture")
    monkeypatch.setattr(adapters.shutil, "which", lambda _: str(binary))
    with adapters._native_attempt() as attempt:
        attempt["deadline"] = time.monotonic() + 600
        if runtime in {"sol", "luna"}:
            native_codex._run_checked(["fixture", "--help"], 15)
            native_codex._run_checked(["fixture", "--version"], 15)
        elif runtime == "flash":
            native_agy._binary(config())
        elif runtime == "grok":
            monkeypatch.setattr(native_cursor, "_resolve_binary", lambda _: binary)
            monkeypatch.setattr(native_cursor, "_auth_contract", lambda _: None)
            native_cursor._probe_cli("fixture", 15)
            native_cursor._assert_login("fixture", 15)
        else:
            monkeypatch.setattr(adapters, "CLAUDE_AUTH_CONTRACT", hashlib.sha256(binary.read_bytes()).hexdigest())
            adapters._claude_capabilities({"claude_bin": str(binary), "timeout_seconds": 600, "max_output_tokens": 8192}, needs_sources=True)
        assert seen
        assert all(cwd == attempt["cwd"] and env == attempt["env"] for cwd, env in seen)
        assert not (attempt["root"] / "reference-journal.jsonl").exists()


@pytest.mark.parametrize("unsafe", ["relative", "leaf-symlink", "ancestor-symlink", "hardlink", "fifo", "directory", "mode", "root-mode", "uid", "empty", "oversize", "mutation", "growth", "rename"])
def test_private_reader_refuses_unsafe_objects_without_streams(tmp_path, monkeypatch, unsafe):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    leaf = write_private(root / "auth.json", b"synthetic-private-data")
    read_root = root
    if unsafe == "relative":
        read_root = Path("relative")
    elif unsafe == "leaf-symlink":
        leaf.unlink()
        leaf.symlink_to(tmp_path / "absent")
    elif unsafe == "ancestor-symlink":
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        read_root = link
    elif unsafe == "hardlink":
        os.link(leaf, root / "linked")
    elif unsafe == "fifo":
        leaf.unlink()
        os.mkfifo(leaf, mode=0o600)
    elif unsafe == "directory":
        leaf.unlink()
        leaf.mkdir(mode=0o600)
    elif unsafe == "mode":
        leaf.chmod(0o644)
    elif unsafe == "root-mode":
        root.chmod(0o755)
    elif unsafe == "uid":
        original = os.fstat
        def foreign(fd):
            value = original(fd)
            if stat.S_ISREG(value.st_mode):
                fields = list(value)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return value
        monkeypatch.setattr(os, "fstat", foreign)
    elif unsafe == "empty":
        leaf.write_bytes(b"")
    elif unsafe == "oversize":
        leaf.write_bytes(b"x" * 1025)
    elif unsafe in {"mutation", "growth", "rename"}:
        original = os.read
        mutated = False
        def change(fd, count):
            nonlocal mutated
            value = original(fd, count)
            if not mutated:
                mutated = True
                if unsafe == "rename":
                    leaf.rename(root / "old")
                    write_private(leaf, b"different")
                else:
                    leaf.write_bytes(b"changed" if unsafe == "mutation" else b"g" * 1025)
            return value
        monkeypatch.setattr(os, "read", change)
    started = time.monotonic()
    with pytest.raises(adapters.AdapterError) as error:
        adapters._checked_private_read(read_root, "auth.json", limit=1024)
    assert time.monotonic() - started < 1
    assert "synthetic-private-data" not in str(error.value)
    assert str(root) not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("relative", ["../escape", "/absolute", "nested/../../escape"])
def test_private_artifact_cannot_escape_root(tmp_path, relative):
    tmp_path.chmod(0o700)
    with pytest.raises(adapters.AdapterError):
        adapters._checked_private_read(tmp_path, relative, limit=16)


def test_access_only_claude_and_cursor_pair_preserve_original(tmp_path):
    source = {"claudeAiOauth": oauth(clientId="do-not-copy", refreshTokenExpiresAt=9999999999999),
              "otherBranch": {"apiKey": "do-not-copy"}}
    root = staging(tmp_path, claude=source, cursor={"accessToken": "synthetic-a", "refreshToken": "synthetic-r",
                                                  "apiKey": "do-not-copy", "bedrockCredentials": {"secret": "do-not-copy"}})
    before = {name: path.read_bytes() for name, path in (("claude", root / ".credentials.json"), ("cursor", root / "auth.json"))}
    for _ in range(2):
        with adapters._native_attempt() as attempt:
            claude = adapters._provision_claude(root, attempt["home"], 600)
            result = json.loads(claude.read_bytes())["claudeAiOauth"]
            assert result["expiresAt"] == source["claudeAiOauth"]["expiresAt"]
            assert set(result) == {"accessToken", "expiresAt", "scopes", "subscriptionType", "rateLimitTier"}
            cursor = native_cursor._provision_cursor(root)
            assert json.loads(cursor.read_bytes()) == {"accessToken": "synthetic-a", "refreshToken": "synthetic-r"}
            assert attempt["env"]["AGENT_CLI_CREDENTIAL_STORE"] == "file"
            assert stat.S_IMODE(cursor.stat().st_mode) == stat.S_IMODE(claude.stat().st_mode) == 0o600
    assert (root / ".credentials.json").read_bytes() == before["claude"]
    assert (root / "auth.json").read_bytes() == before["cursor"]


@pytest.mark.parametrize("bad", [None, [], {}, {"claudeAiOauth": []}, {"claudeAiOauth": oauth(expiresAt=True)},
    {"claudeAiOauth": oauth(expiresAt="9999999999999")}, {"claudeAiOauth": oauth(expiresAt=0)},
    {"claudeAiOauth": oauth(expiresAt=float("inf"))}, {"claudeAiOauth": oauth(accessToken="")},
    {"claudeAiOauth": oauth(accessToken="has space")}, {"claudeAiOauth": oauth(scopes="string")},
    {"claudeAiOauth": oauth(scopes=[False])}, {"claudeAiOauth": oauth(subscriptionType=False)},
    {"claudeAiOauth": oauth(rateLimitTier={})}, b'{"claudeAiOauth":{},"claudeAiOauth":{}}',
    b'{"claudeAiOauth":{"accessToken":"a","accessToken":"b"}}', b'\xff'])
def test_claude_malformed_staging_is_not_run(tmp_path, bad):
    root = staging(tmp_path, claude=bad) if bad is not None else None
    with adapters._native_attempt() as attempt, pytest.raises(adapters.AuthUnavailable, match="^auth_unavailable$"):
        adapters._provision_claude(root, attempt["home"], 600)


@pytest.mark.parametrize("bad", [{}, {"accessToken":"a"}, {"accessToken": "", "refreshToken": "r"},
    {"accessToken": "a", "refreshToken": False}, {"accessToken": "a", "refreshToken": ""},
    b'{"accessToken":"a","refreshToken":"r","refreshToken":"x"}'])
def test_cursor_access_only_or_malformed_staging_is_not_run(tmp_path, bad):
    root = staging(tmp_path, cursor=bad)
    with adapters._native_attempt(), pytest.raises(adapters.AuthUnavailable, match="^auth_unavailable$"):
        native_cursor._provision_cursor(root)


def test_expiry_boundary_and_exclusive_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000)
    root = staging(tmp_path, claude={"claudeAiOauth": oauth(expiresAt=(1000+600+300+60)*1000)})
    with adapters._native_attempt() as attempt:
        with pytest.raises(adapters.AuthUnavailable):
            adapters._provision_claude(root, attempt["home"], 600)
        write_private(root / ".credentials.json", {"claudeAiOauth": oauth(expiresAt=(1000+600+300+61)*1000)})
        path = adapters._provision_claude(root, attempt["home"], 600)
        original = path.read_bytes()
        with pytest.raises(FileExistsError):
            adapters._exclusive_private_write(path, b"cannot replace")
        assert path.read_bytes() == original


@pytest.mark.parametrize("rotate", [False, True])
def test_child_auth_integrity_on_success_or_exception(tmp_path, rotate):
    records = []
    with adapters._native_attempt() as attempt:
        path = write_private(attempt["home"] / "auth.json", b"initial")
        if rotate:
            with pytest.raises(adapters.AuthUnavailable), adapters._auth_integrity(path, lambda *event: records.append(event)):
                path.write_bytes(b"rotated-secret")
                raise RuntimeError("native failure")
        else:
            with adapters._auth_integrity(path, lambda *event: records.append(event)):
                pass
    assert records == [("native_auth_integrity", {"rotated": rotate, "operator_alert": rotate})]
    assert "secret" not in json.dumps(records)


def test_installed_auth_contract_guards_refuse_drift(tmp_path, monkeypatch):
    binary = write_private(tmp_path / "cli", b"public-fixture")
    expected = hashlib.sha256(b"public-fixture").hexdigest()
    monkeypatch.setattr(native_cursor, "_CURSOR_AUTH_CONTRACT", {"index.js": expected})
    with pytest.raises(adapters.AdapterError, match="unavailable"):
        native_cursor._auth_contract(binary)
    write_private(tmp_path / "index.js", b"public-fixture")
    native_cursor._auth_contract(binary)
    (tmp_path / "index.js").write_bytes(b"changed-source")
    with pytest.raises(adapters.AdapterError, match="changed"):
        native_cursor._auth_contract(binary)


@pytest.mark.parametrize("kind", ["cli_result", "cli_timeout"])
def test_auth_diagnostics_sanitized_but_post_model_evidence_preserved(kind):
    records = []
    callback = adapters._auth_capture(lambda *event: records.append(event))
    raw = {"stdout": "login failed secret-access /private/path account@example.invalid", "stderr": "oauth", "returncode": 1}
    callback(kind, raw)
    assert records[-1][1] == {"reason":"auth_unavailable", "returncode":1, "timeout":kind == "cli_timeout"}
    raw = {"stdout": '{"type":"thinking","session_id":"native","model":"Grok"}\n', "stderr":"", "returncode":-9}
    callback(kind, raw)
    assert records[-1][1] is raw
    assert adapters.normalized_reason(adapters.AuthUnavailable("auth_unavailable")) == "auth_unavailable"


def test_missing_staging_is_typed_not_run_and_never_retries(monkeypatch):
    from test_zno_nmt_runner import _config, _packet
    monkeypatch.setattr(runner, "preflight", lambda *_: (_ for _ in ()).throw(adapters.AuthUnavailable("auth_unavailable")))
    calls = []
    monkeypatch.setattr(adapters, "run_claude", lambda *_a, **_kw: calls.append(1))
    monkeypatch.setattr(runner, "_validated_packet", lambda value: value)
    value = _config(adapter="claude")
    value.pop("endpoint_env", None)
    value.pop("key_env", None)
    result = runner.run_exam(_packet(), value, "closed-book")
    assert result["status"] == "failed" and result["execution_disposition"] == "NOT_RUN"
    assert result["failure_reason"] == "auth_unavailable" and calls == []


def test_frozen_catalog_matches_artifact_and_refuses_drift():
    raw = CATALOG_FILE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "b29924c3f0ea434a0328b3080b8dd6d741adfe94a923b10629a37672dd194026"
    tools = json.loads(raw)
    normalized = adapters._reference_catalog(tools, adapters.SMOKE_TOOLS)
    assert adapters.digest(normalized) == adapters.SMOKE_CATALOG_SHA256
    assert [item["name"] for item in normalized] == adapters.SMOKE_TOOLS
    tools[0]["description"] = "changed"
    with pytest.raises(adapters.AdapterError, match="frozen"):
        adapters._reference_catalog(tools, adapters.SMOKE_TOOLS)


@pytest.mark.parametrize("native", [False, True])
def test_five_schema_valid_content_calls_and_metadata_caps(monkeypatch, native):
    catalog = adapters._reference_catalog(json.loads(CATALOG_FILE.read_bytes()), adapters.SMOKE_TOOLS)
    bridge = ReferenceBridge("unused", adapters.SMOKE_TOOLS, timeout=1, max_tool_calls=20) if native else proxy.Bridge("unused", adapters.SMOKE_TOOLS)
    requests = []
    def upstream(method, params, ident=1):
        requests.append((method, params))
        body = {"protocolVersion":"2025-06-18", "serverInfo":{"name":"fixture", "version":"1"}} if method == "initialize" else ({"tools": catalog} if method == "tools/list" else {"content": []})
        return {"jsonrpc":"2.0", "id":ident, "result":body}
    monkeypatch.setattr(bridge, "request", upstream)
    bridge.handle({"jsonrpc":"2.0", "id":1, "method":"initialize", "params":{}})
    bridge.handle({"jsonrpc":"2.0", "id":2, "method":"tools/list"})
    arguments = [{"words":["fixture"]}, {"word":"fixture"}, {"topic":"fixture"}, {"query":"fixture"}, {"query":"fixture"}]
    for index, (name, args) in enumerate(zip(adapters.SMOKE_TOOLS, arguments, strict=True)):
        assert bridge.handle({"jsonrpc":"2.0", "id":10+index, "method":"tools/call", "params":{"name":name,"arguments":args}})["result"] == {"content":[]}
    assert bridge.calls == 5
    assert bridge.handle({"jsonrpc":"2.0", "id":20, "method":"resources/list"})["error"]["code"] == -32601
    assert "resources/list" not in [method for method, _ in requests]
    assert bridge.metadata_operations == 3
    for _ in range(5):
        bridge.handle({"jsonrpc":"2.0", "id":21, "method":"ping"})
    with pytest.raises(ValueError, match="metadata limit"):
        bridge.handle({"jsonrpc":"2.0", "id":22, "method":"ping"})
    assert bridge.calls == 5


@pytest.mark.parametrize("value,schema", [(True, {"type":"integer"}), ({}, {"type":"object","required":["query"]}),
    ({"query":3}, {"type":"object","properties":{"query":{"type":"string"}}}),
    ([1], {"type":"array","items":{"type":"string"}}), ("foreign", {"type":"string","enum":["sources"]}),
    ({"extra":1}, {"type":"object","additionalProperties":False}), (0, {"type":"integer","minimum":1}),
    (10, {"type":"integer","maximum":2}), ("", {"type":"string","minLength":1}),
    ("long", {"type":"string","maxLength":1}), ([], {"type":"array","minItems":1}),
    ([1,2], {"type":"array","maxItems":1}), (None, {"$ref":"unknown"})])
def test_argument_schema_refusals(value, schema):
    with pytest.raises(ValueError):
        proxy.validate_arguments(value, schema)


@pytest.mark.parametrize("args", [{"ServerName":"other"}, {"ServerName":"sources","uri":"secret"}, {}, {"ServerName":"sources","ToolName":"other"}])
def test_agy_metadata_is_scoped_and_closed_book_denied(args):
    controls = {"deadline":time.monotonic()+10, "tools":["verify_words"], "max_tool_calls":20, "max_metadata_operations":8}
    assert agy_hook.decide({"name":"list_resources", "args":args}, controls, 0) == (False, False)
    controls["tools"] = []
    assert agy_hook.decide({"name":"list_resources", "args":{"ServerName":"sources"}}, controls, 0) == (False,False)


def test_agy_metadata_unsupported_result_is_native_only_and_not_content():
    controls = {"deadline":time.monotonic()+10, "tools":["verify_word"], "max_tool_calls":20, "max_metadata_operations":8}
    assert agy_hook.decide({"name":"list_resources","args":{"ServerName":"sources"}}, controls, 0, 7) == (True,False)
    assert agy_hook.decide({"name":"list_resources","args":{"ServerName":"sources"}}, controls, 0, 8) == (False,False)
    value = events(False)
    value.insert(2, {"event":"step_update", "step_update":{"conversation_id":"session", "step_index":3,"state":"DONE","step_type":"tool", "tool_name":"list_resources", "tool_info":{"parameters":{"ServerName":"sources"},"output":"unsupported"}}})
    hooks = [{"decision":"allow","call":{"name":"list_resources","args":{"ServerName":"sources"}}}, *hook_receipts()]
    result = native_agy.parse_events(serialize(value), packet(), config(), hooks, [])
    assert result["metrics"]["native_metadata_operations"] == 1 and result["metrics"]["tool_calls"] == 0


@pytest.mark.parametrize("failure", ["missing", "failed", "swapped", "changed-arguments", "duplicate", "conflicting"])
def test_native_controller_content_equality_never_backfills(failure):
    controller = [{"name":"search_text","arguments":{"query":"first"},"result":{"content":[{"type":"text","text":"first result"}]}},
                  {"name":"search_text","arguments":{"query":"second"},"result":{"content":[{"type":"text","text":"second result"}]}}]
    native = [{"name":call["name"],"arguments":call["arguments"],"output":call["result"]} for call in controller]
    adapters._attest_content_calls(native, controller)
    if failure == "missing":
        native.pop()
    elif failure == "failed":
        controller[0]["result"]["isError"] = True
    elif failure == "swapped":
        native[0]["output"], native[1]["output"] = native[1]["output"], native[0]["output"]
    elif failure == "changed-arguments":
        native[0]["arguments"] = {"query":"changed"}
    elif failure == "duplicate":
        controller[1] = copy.deepcopy(controller[0])
        native[1] = copy.deepcopy(native[0])
    else:
        native[0]["output"] = "invented"
    with pytest.raises(adapters.AdapterError):
        adapters._attest_content_calls(native, controller)


@pytest.mark.parametrize("failure", ["failed", "missing", "unfinished", "duplicate", "arguments", "swapped"])
def test_cursor_completed_native_calls_require_positive_evidence(failure):
    base = _stream_events()
    start = {"type":"tool_call","subtype":"started","call_id":"one","session_id":"fixture-session", "tool_call":{"mcpToolCall":{"args":{"serverIdentifier":"sources","name":"sources-verify_word","args":{"word":"fixture"}}}}}
    end = copy.deepcopy(start)
    end["subtype"] = "completed"
    end["tool_call"]["mcpToolCall"]["result"] = {"success":{"content":"REFERENCE"}}
    values = [base[0],start,end,*base[1:]]
    parsed = native_cursor._parse_stream_envelope(serialize(values), _packet(), {"verify_word"}, 20)
    assert parsed.tool_calls == 1
    if failure == "failed":
        end["tool_call"]["mcpToolCall"]["result"] = {"error":"native error"}
    elif failure == "missing":
        del end["tool_call"]["mcpToolCall"]["result"]
    elif failure == "unfinished":
        values.remove(end)
    elif failure == "duplicate":
        values.insert(3,copy.deepcopy(end))
    elif failure == "arguments":
        end["tool_call"]["mcpToolCall"]["args"]["args"] = {"word":"changed"}
    else:
        end["call_id"] = "swapped"
    with pytest.raises(adapters.AdapterError):
        native_cursor._parse_stream_envelope(serialize(values), _packet(), {"verify_word"}, 20)


@pytest.mark.parametrize("failure", ["duplicate", "changed-arguments", "output", "failed"])
def test_agy_completion_cannot_override_previous_native_evidence(failure):
    values = events(True)
    step = copy.deepcopy(values[2])
    if failure == "duplicate":
        values.insert(3,step)
    elif failure == "changed-arguments":
        values[2]["step_update"]["state"] = "ACTIVE"
        step["step_update"]["tool_info"]["parameters"]["Arguments"] = {"word":"changed"}
        values.insert(3,step)
    elif failure == "output":
        values[2]["step_update"]["state"] = "ACTIVE"
        step["step_update"]["tool_info"]["output"] = "changed"
        values.insert(3,step)
    else:
        receipts = call_receipts(True)
        receipts[0]["result"]["isError"] = True
        with pytest.raises(adapters.AdapterError):
            native_agy.parse_events(serialize(values), packet(), config(), hook_receipts(True), receipts)
        return
    with pytest.raises(adapters.AdapterError):
        native_agy.parse_events(serialize(values), packet(), config(), hook_receipts(True), call_receipts(True))


def capture_fixture(attempt):
    root = attempt["root"]
    app_data = attempt["home"] / ".gemini/antigravity-cli"
    app_data.mkdir(parents=True, mode=0o700)
    for directory in app_data.parents:
        if directory == root:
            break
        directory.chmod(0o700)
    session = emit_agy_capture(root)
    return root, root / "native-log.txt", app_data, session


def test_agy_bound_full_artifacts_retained_before_cleanup():
    records = []
    with adapters._native_attempt() as attempt:
        root, log, app_data, session = capture_fixture(attempt)
        transcript = app_data / "brain" / session / ".system_generated/logs/transcript.jsonl"
        result = app_data / "brain" / session / "steps/one/output.txt"
        result.parent.mkdir(parents=True,mode=0o700)
        result.parent.parent.chmod(0o700)
        write_private(result, b"full result" * 400000)
        write_private(transcript, (json.dumps({"type":"GENERIC","content":result.as_uri()})+"\n").encode())
        assert native_agy.capture_native_artifacts(root,log,app_data,lambda *record:records.append(record)) == session
        assert result.exists()
    assert not root.exists()
    artifacts = [payload for kind,payload in records if kind == "agy_native_artifact"]
    assert [item["kind"] for item in artifacts] == ["log","transcript","tool-result"]
    assert base64.b64decode(artifacts[-1]["raw_base64"]) == b"full result" * 400000
    assert artifacts[-1]["sha256"] == hashlib.sha256(b"full result" * 400000).hexdigest()
    assert records[-1][1]["diagnostic_only"] is True


@pytest.mark.parametrize("failure", ["missing-log", "duplicate-conversation", "wrong-conversation", "missing-transcript", "truncated", "malformed", "overbound", "symlink", "hardlink", "pointer-escape"])
def test_agy_artifacts_fail_closed_and_preserve_available_diagnostics(failure):
    records = []
    with adapters._native_attempt() as attempt:
        root,log,app_data,session = capture_fixture(attempt)
        transcript = app_data / "brain" / session / ".system_generated/logs/transcript.jsonl"
        if failure == "missing-log":
            log.unlink()
        elif failure == "duplicate-conversation":
            log.write_text(log.read_text()*2)
        elif failure == "wrong-conversation":
            log.write_text("Created conversation 00000000-0000-0000-0000-000000000002\n")
        elif failure == "missing-transcript":
            transcript.unlink()
        elif failure == "truncated":
            transcript.write_bytes(b'{"type":"GENERIC"}')
        elif failure == "malformed":
            transcript.write_bytes(b'{invalid\n')
        elif failure == "overbound":
            with transcript.open("r+b") as stream:
                stream.truncate(native_agy.ARTIFACT_LIMIT+1)
        elif failure == "symlink":
            transcript.unlink()
            transcript.symlink_to(log)
        elif failure == "hardlink":
            os.link(transcript,log.parent/"extra-link")
        elif failure == "pointer-escape":
            transcript.write_text(json.dumps({"type":"GENERIC","content":(root/"home/secret").as_uri()})+"\n")
        with pytest.raises(adapters.AdapterError):
            native_agy.capture_native_artifacts(root,log,app_data,lambda *record:records.append(record))
        assert records[-1][0] == "agy_native_capture_failure"
    assert all("raw_base64" not in payload or payload["kind"] in {"log","transcript"} for _kind,payload in records)


def test_agy_missing_inline_result_remains_failed_even_with_capture():
    value = events(True)
    del value[2]["step_update"]["tool_info"]["output"]
    with pytest.raises(adapters.AdapterError, match="result evidence mismatch"):
        native_agy.parse_events(serialize(value),packet(),config(),hook_receipts(True),call_receipts(True))


@pytest.mark.parametrize("content_first", [False, True])
def test_hook_main_keeps_discovery_and_content_counters_independent(tmp_path, monkeypatch, capsys, content_first):
    import io
    controls = {"tools": ["verify_words"], "max_tool_calls": 1,
                "max_metadata_operations": 8, "deadline": time.monotonic() + 600}
    path = write_private(tmp_path / "gate.json", controls)
    monkeypatch.setattr(agy_hook.sys, "argv", ["hook", str(path)])
    content = {"name": "call_mcp_tool", "args": {"ServerName": "sources", "ToolName": "verify_words", "Arguments": {"words": ["synthetic"]}}}
    metadata = {"name": "list_resources", "args": {"ServerName": "sources"}}
    calls = ([content] if content_first else []) + [metadata] * 9 + ([] if content_first else [content]) + [content]
    for call in calls:
        monkeypatch.setattr(agy_hook.sys, "stdin", io.StringIO(json.dumps({"toolCall": call})))
        assert agy_hook.main() == 0
    outputs = [json.loads(line)["decision"] for line in capsys.readouterr().out.splitlines()]
    assert outputs.count("allow") == 9 and outputs.count("deny") == 2
    assert json.loads(path.with_suffix(".state").read_text()) == {"content": 1, "metadata": 8}
    receipts = [json.loads(line) for line in path.with_suffix(".jsonl").read_text().splitlines()]
    assert receipts[-1]["count_before"] == 1 and receipts[-1]["metadata_count_before"] == 8
    monkeypatch.setattr(agy_hook.sys, "stdin", io.StringIO("malformed"))
    assert agy_hook.main() == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "deny"


def test_runner_preflight_preserves_typed_auth_failure(monkeypatch):
    from ukrainian_llm_eval.core import ExamError
    monkeypatch.setattr(adapters, "preflight", lambda *_: (_ for _ in ()).throw(adapters.AuthUnavailable("unavailable")))
    with pytest.raises(ExamError, match="^auth_unavailable$"):
        runner.preflight({}, "closed-book")


@pytest.mark.parametrize("runtime", ["claude", "cursor"])
def test_probe_auth_mutation_stops_before_candidate(tmp_path, monkeypatch, runtime):
    from test_zno_nmt_runner import _config
    root = staging(tmp_path, claude={"claudeAiOauth": oauth()}, cursor={"accessToken": "fixture", "refreshToken": "fixture"})
    original = {p.name: p.read_bytes() for p in root.iterdir()}
    monkeypatch.setenv("UKRAINIAN_LLM_EVAL_CLAUDE_PROVISIONING_DIR", str(root))
    monkeypatch.setenv("UKRAINIAN_LLM_EVAL_CURSOR_PROVISIONING_DIR", str(root))
    def mutate(*_args, **_kwargs):
        attempt = adapters._ATTEMPT.get()
        path = attempt["home"] / ".claude/.credentials.json" if runtime == "claude" else Path(attempt["env"]["XDG_CONFIG_HOME"]) / "cursor/auth.json"
        path.write_bytes(b"unexpected rotation")
        raise RuntimeError("capability probe failed")
    config_value = _config(adapter="claude")
    config_value.pop("endpoint_env", None)
    config_value.pop("key_env", None)
    if runtime == "claude":
        monkeypatch.setattr(adapters, "_claude_capabilities", mutate)
        def target():
            return adapters.run_claude(packet(), config_value, "closed-book", sources_url=None, prompt="fixture")
    else:
        from test_native_cursor import _config as cursor_config
        monkeypatch.setattr(native_cursor, "_probe_cli", mutate)
        def target():
            return native_cursor.run_cursor(packet(), cursor_config(str(write_private(tmp_path / "binary", b"fixture"))), "closed-book", prompt="fixture")
    monkeypatch.setattr(adapters, "_run_claude_process", lambda *_a, **_k: pytest.fail("candidate must not run"))
    monkeypatch.setattr(native_cursor, "_run_cursor_process", lambda *_a, **_k: pytest.fail("candidate must not run"))
    with pytest.raises(adapters.AuthUnavailable):
        target()
    assert {p.name: p.read_bytes() for p in root.iterdir()} == original


def test_claude_overflowing_expiry_is_sanitized(tmp_path):
    root = staging(tmp_path, claude={"claudeAiOauth": oauth(expiresAt=10**1000)})
    with adapters._native_attempt() as attempt, pytest.raises(adapters.AuthUnavailable):
        adapters._provision_claude(root, attempt["home"], 600)


@pytest.mark.parametrize("failure", [None, "truncated", "bad-row", "rpc-error", "is-error", "wrong-id", "wrong-name", "wrong-args", "forbidden-method", "discovery-args", "metadata-cap"])
def test_controller_journal_requires_complete_real_rpc_and_scoped_metadata(failure):
    catalog = [{"name": "verify_word", "inputSchema": {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]}}]
    call = {"request": {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "verify_word", "arguments": {"word": "fixture"}}},
            "response": {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}}
    metadata = {"request": {"jsonrpc": "2.0", "id": 2, "method": "resources/list"}, "response": {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601}}}
    rows = [metadata, call]
    if failure == "bad-row":
        rows.append({"request": None})
    elif failure == "rpc-error":
        call["response"] = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603}}
    elif failure == "is-error":
        call["response"]["result"]["isError"] = True
    elif failure == "wrong-id":
        call["response"]["id"] = 10
    elif failure == "wrong-name":
        call["request"]["params"]["name"] = "foreign"
    elif failure == "wrong-args":
        call["request"]["params"]["arguments"] = {"word": 1}
    elif failure == "forbidden-method":
        metadata["request"]["method"] = "resources/read"
    elif failure == "discovery-args":
        metadata["request"]["params"] = {"server": "foreign"}
    elif failure == "metadata-cap":
        rows = [metadata] * 9
    with adapters._native_attempt() as attempt:
        raw = ("\n".join(json.dumps(row) for row in rows) + ("" if failure == "truncated" else "\n")).encode()
        write_private(attempt["root"] / "reference-journal.jsonl", raw)
        if failure:
            with pytest.raises(adapters.AdapterError):
                adapters._controller_evidence(attempt["root"], catalog, None)
        else:
            calls, count = adapters._controller_evidence(attempt["root"], catalog, None)
            assert count == 1 and calls == [{"name": "verify_word", "arguments": {"word": "fixture"}, "result": {"content": []}, "id": 1}]


@pytest.mark.parametrize("failure", [None, "duplicate-call", "duplicate-result", "missing-result", "failed-result", "unfinished"])
def test_claude_native_ids_and_completed_result_are_unique(failure):
    start = {"type": "tool_use", "id": "unique", "name": "mcp__sources__verify_word", "input": {"word": "fixture"}}
    end = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "unique", "content": []}]}}
    rows = [start, end]
    if failure == "duplicate-call":
        rows += [start, end]
    elif failure == "duplicate-result":
        rows.append(end)
    elif failure == "missing-result":
        del end["message"]["content"][0]["content"]
    elif failure == "failed-result":
        end["message"]["content"][0]["is_error"] = True
    elif failure == "unfinished":
        rows.pop()
    raw = "\n".join(json.dumps(row) for row in rows)
    if failure:
        with pytest.raises(adapters.AdapterError):
            adapters._claude_content_calls(raw)
    else:
        assert adapters._claude_content_calls(raw) == [{"name": "verify_word", "arguments": {"word": "fixture"}, "output": []}]


@pytest.mark.parametrize("native", [False, True])
def test_exhausted_discovery_remains_in_controller_custody(tmp_path, native):
    journal = write_private(tmp_path / "journal.jsonl", b"")
    bridge = ReferenceBridge("unused", ["verify_word"], timeout=1, max_tool_calls=20, journal=journal) if native else proxy.Bridge("unused", ["verify_word"], journal=journal)
    bridge.metadata_operations = 8
    request = {"jsonrpc": "2.0", "id": 9, "method": "ping"}
    with pytest.raises(ValueError):
        bridge.handle(request)
    record = json.loads(journal.read_text())
    if native:
        assert record == {"event": "rejected", "method": "ping", "forwarded": False}
        from ukrainian_llm_eval import codex_reference
        with pytest.raises(adapters.AdapterError, match="controller evidence"):
            codex_reference.parse_events("", {}, [], [record])
    else:
        assert record["request"] == request and record["response"]["error"]["code"] == -32603


@pytest.mark.parametrize("failure", ["timeout", "native-error", "auth-unavailable"])
def test_agy_failure_captures_bound_artifacts_before_cleanup(tmp_path, monkeypatch, failure):
    from test_native_agy import provision
    staged = provision(tmp_path)
    binary = write_private(tmp_path / "binary", b"public fixture")
    monkeypatch.setattr(native_agy, "_binary", lambda _: (str(binary), hashlib.sha256(binary.read_bytes()).hexdigest()))
    records, roots = [], []
    def run(argv, **kwargs):
        root = kwargs["cwd"].parent
        roots.append(root)
        emit_agy_capture(root)
        if failure == "auth-unavailable":
            raise adapters.AuthUnavailable("auth_unavailable")
        if failure == "timeout":
            raise adapters.AdapterError("CLI timeout")
        return subprocess.CompletedProcess(argv, 1, '{"type":"thinking"}', "native failure")
    monkeypatch.setattr(adapters, "_run_claude_process", run)
    with pytest.raises(adapters.AdapterError):
        native_agy.run_agy(packet(), config(), "closed-book", sources_url=None, prompt="fixture", private_env_path=staged,
                           evidence=lambda *record: records.append(record))
    artifacts = [payload for kind, payload in records if kind == "agy_native_artifact"]
    assert len(roots) == 1 and not roots[0].exists()
    assert [row["kind"] for row in artifacts] == ([] if failure == "auth-unavailable" else ["log", "transcript"])
    assert all(base64.b64decode(row["raw_base64"]) for row in artifacts)


@pytest.mark.parametrize("adapter", ["chat-http", "responses-http"])
def test_http_preflight_does_not_require_native_filesystem_controls(monkeypatch, adapter):
    from test_zno_nmt_runner import _config
    config_value = _config(adapter=adapter)
    monkeypatch.setenv(config_value["endpoint_env"], "https://fixture.invalid/unused")
    monkeypatch.setattr(adapters, "_native_attempt", lambda: pytest.fail("HTTP preflight must not create a native attempt"))
    receipt = adapters.preflight(config_value, "closed-book")
    assert receipt["capability"] == adapter + "-controller-mediated"


@pytest.mark.parametrize("runtime", ["claude", "cursor"])
def test_probe_timeout_never_exports_private_auth_exception_chain(tmp_path, monkeypatch, runtime):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("fixture", 1, output="synthetic-auth-secret", stderr="synthetic-private-location")
    monkeypatch.setattr(subprocess, "run", timeout)
    binary = write_private(tmp_path / "binary", b"public fixture")
    monkeypatch.setattr(native_cursor, "_resolve_binary", lambda _: binary)
    monkeypatch.setattr(native_cursor, "_auth_contract", lambda _: None)
    with pytest.raises(adapters.AdapterError) as error:
        if runtime == "claude":
            adapters._run_checked(["fixture"], timeout=1)
        else:
            native_cursor._probe_cli("fixture", 1)
    assert error.value.__cause__ is None and error.value.__suppress_context__ is True
    assert "synthetic" not in str(error.value)
