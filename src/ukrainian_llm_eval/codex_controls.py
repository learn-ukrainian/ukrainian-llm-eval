"""Capture local-only evidence for native Codex closed-book handler controls.

This module runs the installed CLI against a loopback Responses fixture.  It
never reads an auth home, supplies credentials, or contacts a provider.  Its
receipt is scoped evidence for one exact fresh noninteractive ``codex exec``
shape, not a claim about interactive hosts, backend identity, effort, or an
output-token bound.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import adapters, native_codex

_TIMEOUT_SECONDS = 18
_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_DECODED_BODY_BYTES = 8 * 1024 * 1024
_MAX_PROCESS_BYTES = 2 * 1024 * 1024
_PROMPT = b"Return a short neutral acknowledgement."
_SAFE_CHILD_ENV = frozenset({"PATH", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"})
_CALL_ID = "call_isolation_probe"
_EXPECTED_FUNCTIONS = frozenset({"exec", "wait", "request_user_input"})
_SYNTHETIC_PACKET = {
    "schema": "zno-nmt.questions.v1",
    "packet_sha256": "0" * 64,
    "items": [{"id": "synthetic-1", "kind": "single", "question": "Synthetic", "options": [], "rows": []}],
}
_HANDLERS = (
    ("functions.exec", "functions", "exec", "custom", 'await tools.exec_command({cmd:"/bin/true"})'),
    (
        "functions.wait",
        "functions",
        "wait",
        "function",
        '{"cell_id":"synthetic_missing_cell","yield_time_ms":1,"max_tokens":1}',
    ),
    (
        "functions.request_user_input",
        "functions",
        "request_user_input",
        "function",
        (
            '{"questions":[{"id":"synthetic","header":"Synthetic","question":"Proceed?",'
            '"options":[{"label":"Continue","description":"Synthetic option"}]}]}'
        ),
    ),
    (
        "delegation",
        "collaboration",
        "spawn_agent",
        "custom",
        '{"task_name":"synthetic","message":"synthetic"}',
    ),
)


class ControlProbeError(RuntimeError):
    """The local fixture could not produce a complete inert-handler receipt."""


@dataclass(frozen=True)
class _HandlerCase:
    receipt_name: str
    namespace: str
    name: str
    envelope: str
    tool_input: str


@dataclass
class _FixtureState:
    root: Path
    handler: _HandlerCase
    posts: list[dict[str, Any]]
    response_events: list[str]
    tool_outputs: list[dict[str, Any]]
    body_error: str | None = None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
    path.chmod(0o600)


def _write_json(path: Path, value: Any) -> None:
    _write_new(path, adapters.canonical(value).encode("utf-8"))


def _decode_body(data: bytes) -> bytes | None:
    if not data.startswith(b"\x1f\x8b"):
        return data
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            decoded = stream.read(_MAX_DECODED_BODY_BYTES + 1)
    except OSError:
        return None
    return decoded if len(decoded) <= _MAX_DECODED_BODY_BYTES else None


def _walk(value: Any):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _request_summary(raw: bytes) -> dict[str, Any]:
    parsed: Any = None
    decoded = _decode_body(raw)
    try:
        if decoded is not None:
            parsed = adapters._strict_json_loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, adapters.AdapterError):
        pass
    top_tools: list[str] = []
    namespaces: dict[str, list[str]] = {}
    surface_valid = True
    top_level_tool_count = 0
    if isinstance(parsed, Mapping):
        raw_top_tools = parsed.get("tools", [])
        if not isinstance(raw_top_tools, list):
            surface_valid = False
            raw_top_tools = []
        top_level_tool_count = len(raw_top_tools)
        for item in raw_top_tools:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str) or not item["name"]:
                surface_valid = False
                continue
            top_tools.append(item["name"])
        inputs = parsed.get("input", [])
        if not isinstance(inputs, list):
            surface_valid = False
            inputs = []
        for item in inputs:
            if not isinstance(item, Mapping) or item.get("type") != "additional_tools":
                continue
            raw_namespaces = item.get("tools")
            if not isinstance(raw_namespaces, list):
                surface_valid = False
                continue
            for namespace in raw_namespaces:
                if (
                    not isinstance(namespace, Mapping)
                    or not isinstance(namespace.get("name"), str)
                    or not namespace["name"]
                    or namespace["name"] in namespaces
                ):
                    surface_valid = False
                    continue
                raw_names = namespace.get("tools")
                if not isinstance(raw_names, list):
                    surface_valid = False
                    continue
                names: list[str] = []
                for tool in raw_names:
                    if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str) or not tool["name"]:
                        surface_valid = False
                        continue
                    names.append(tool["name"])
                if len(names) != len(set(names)):
                    surface_valid = False
                namespaces[namespace["name"]] = names
    return {
        "raw_bytes": len(raw),
        "raw_sha256": _sha256(raw),
        "decoded_within_bound": decoded is not None,
        "json_object": isinstance(parsed, Mapping),
        "tool_surface_valid": surface_valid,
        "top_level_tool_count": top_level_tool_count,
        "top_level_tool_names": top_tools,
        "additional_tool_namespaces": namespaces,
    }


def _response(identifier: str, model: str, output: list[dict[str, Any]], status: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": False,
        "previous_response_id": None,
        "reasoning": {"effort": "medium", "summary": None},
        "store": False,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": None,
        "metadata": {},
    }


def _sse_event(kind: str, data: Mapping[str, Any], sequence: int) -> bytes:
    payload = dict(data)
    payload.setdefault("type", kind)
    payload.setdefault("sequence_number", sequence)
    return f"event: {kind}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def _tool_events(case: _HandlerCase, model: str) -> list[tuple[str, dict[str, Any]]]:
    item_id, call_id = "ctc_isolation_probe", _CALL_ID
    if case.envelope == "function":
        done = {
            "id": item_id,
            "type": "function_call",
            "status": "completed",
            "call_id": call_id,
            "name": case.name,
            "arguments": case.tool_input,
        }
        active = {**done, "status": "in_progress", "arguments": ""}
        return [
            ("response.created", {"response": _response("resp_1", model, [], "in_progress")}),
            ("response.output_item.added", {"output_index": 0, "item": active}),
            ("response.function_call_arguments.delta", {"output_index": 0, "item_id": item_id, "delta": case.tool_input}),
            ("response.function_call_arguments.done", {"output_index": 0, "item_id": item_id, "arguments": case.tool_input}),
            ("response.output_item.done", {"output_index": 0, "item": done}),
            ("response.completed", {"response": _response("resp_1", model, [done], "completed")}),
        ]
    done = {
        "id": item_id,
        "type": "custom_tool_call",
        "status": "completed",
        "call_id": call_id,
        "name": case.name,
        "namespace": case.namespace,
        "input": case.tool_input,
    }
    active = {**done, "status": "in_progress", "input": ""}
    return [
        ("response.created", {"response": _response("resp_1", model, [], "in_progress")}),
        ("response.output_item.added", {"output_index": 0, "item": active}),
        ("response.custom_tool_call_input.delta", {"output_index": 0, "item_id": item_id, "delta": case.tool_input}),
        ("response.custom_tool_call_input.done", {"output_index": 0, "item_id": item_id, "input": case.tool_input}),
        ("response.output_item.done", {"output_index": 0, "item": done}),
        ("response.completed", {"response": _response("resp_1", model, [done], "completed")}),
    ]


def _final_events(model: str) -> list[tuple[str, dict[str, Any]]]:
    item = {
        "id": "msg_final",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Synthetic probe completed.", "annotations": []}],
    }
    return [
        ("response.created", {"response": _response("resp_2", model, [], "in_progress")}),
        ("response.output_item.added", {"output_index": 0, "item": {**item, "status": "in_progress", "content": []}}),
        ("response.output_item.done", {"output_index": 0, "item": item}),
        ("response.completed", {"response": _response("resp_2", model, [item], "completed")}),
    ]


class _LoopbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _state(self) -> _FixtureState:
        return self.server.state  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = adapters.canonical(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        state = self._state()
        body = b"".join(_sse_event(kind, item, index) for index, (kind, item) in enumerate(events, start=1))
        state.response_events.extend(kind for kind, _item in events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send_json(404, {"error": {"type": "synthetic_no_get"}})

    def do_POST(self) -> None:
        state = self._state()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > _MAX_BODY_BYTES:
            state.body_error = "loopback request body exceeds bound"
            self._send_json(413, {"error": {"type": "synthetic_body_rejected"}})
            return
        raw = self.rfile.read(length)
        request_path = state.root / f"request_body_{len(state.posts) + 1:02d}.bin"
        _write_new(request_path, raw)
        state.posts.append(_request_summary(raw))
        decoded = _decode_body(raw)
        if decoded is None:
            state.body_error = "loopback decoded request body exceeds bound or is invalid"
            parsed = None
        else:
            try:
                parsed = adapters._strict_json_loads(decoded.decode("utf-8"))
            except (UnicodeDecodeError, adapters.AdapterError):
                parsed = None
        if not isinstance(parsed, Mapping):
            parsed = None
        else:
            for value in _walk(parsed):
                if not isinstance(value, Mapping) or value.get("type") not in {
                    "custom_tool_call_output", "function_call_output",
                }:
                    continue
                output = value.get("output")
                raw_output = (
                    output.encode("utf-8") if isinstance(output, str) else adapters.canonical(output).encode("utf-8")
                )
                state.tool_outputs.append(
                    {
                        "type": value["type"],
                        "call_id": value.get("call_id"),
                        "sha256": _sha256(raw_output),
                        "text": output,
                    }
                )
        model = self.server.model  # type: ignore[attr-defined]
        if len(state.posts) == 1:
            self._stream(_tool_events(state.handler, model))
        else:
            self._stream(_final_events(model))

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                return


def _bounded_process_output(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes, bool, bool]:
    """Write stdin and drain both pipes without retaining unbounded output."""

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    lock = threading.Lock()

    def drain(name: str, stream: Any) -> None:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                return
            with lock:
                remaining = _MAX_PROCESS_BYTES - len(captured[name])
                if remaining > 0:
                    captured[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    _terminate(process)

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        if process.stdin is not None:
            process.stdin.write(_PROMPT)
            process.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    timed_out = False
    try:
        process.wait(timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
        process.wait()
    for reader in readers:
        reader.join(timeout=3)
    return bytes(captured["stdout"]), bytes(captured["stderr"]), timed_out, overflow.is_set()


def _expected_output(case: _HandlerCase, outputs: list[dict[str, Any]]) -> bool:
    expected_type = "function_call_output" if case.envelope == "function" else "custom_tool_call_output"
    texts = [
        value.get("text")
        for value in outputs
        if value.get("type") == expected_type and value.get("call_id") == _CALL_ID
    ]
    if not any(isinstance(text, str) for text in texts):
        return False
    if case.receipt_name in {"functions.exec", "functions.wait"}:
        return any("code-mode host is disabled" in text.casefold() for text in texts if isinstance(text, str))
    if case.receipt_name == "functions.request_user_input":
        return any(
            text.casefold() == "request_user_input is unavailable in default mode"
            for text in texts
            if isinstance(text, str)
        )
    return any(text.casefold().startswith("unsupported custom tool call:") for text in texts if isinstance(text, str))


def _advertisement_matches(case: _HandlerCase, first_request: Mapping[str, Any] | None) -> bool:
    if not isinstance(first_request, Mapping):
        return False
    namespaces = first_request.get("additional_tool_namespaces")
    top_tools = first_request.get("top_level_tool_names")
    if (
        first_request.get("tool_surface_valid") is not True
        or first_request.get("top_level_tool_count") != 0
        or not isinstance(namespaces, Mapping)
        or not isinstance(top_tools, list)
    ):
        return False
    functions = namespaces.get("functions")
    if top_tools or set(namespaces) != {"functions"} or not isinstance(functions, list):
        return False
    return len(functions) == len(_EXPECTED_FUNCTIONS) and set(functions) == _EXPECTED_FUNCTIONS


def _loopback_overrides(base_url: str) -> tuple[str, ...]:
    return (
        "-c", 'model_provider="loopback_only_provider"',
        "-c", 'model_providers.loopback_only_provider.name="loopback-only-provider"',
        "-c", f"model_providers.loopback_only_provider.base_url={json.dumps(base_url)}",
        "-c", 'model_providers.loopback_only_provider.wire_api="responses"',
        "-c", "model_providers.loopback_only_provider.requires_openai_auth=false",
        "-c", "model_providers.loopback_only_provider.request_max_retries=0",
        "-c", "model_providers.loopback_only_provider.stream_max_retries=0",
        "-c", "model_providers.loopback_only_provider.stream_idle_timeout_ms=1000",
    )


def _run_case(root: Path, probe: native_codex._CliProbe, model: str, effort: str, case: _HandlerCase) -> dict[str, Any]:
    case_root = root / case.receipt_name.replace(".", "-")
    case_root.mkdir(mode=0o700)
    state = _FixtureState(case_root, case, [], [], [])
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    server.state = state  # type: ignore[attr-defined]
    server.model = model  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stdout = b""
    stderr = b""
    returncode: int | None = None
    timed_out = False
    error: str | None = None
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="codex-control-home-") as home_raw, tempfile.TemporaryDirectory(
            prefix="codex-control-cwd-"
        ) as cwd_raw:
            home = Path(home_raw)
            cwd = Path(cwd_raw)
            home.chmod(0o700)
            cwd.chmod(0o700)
            codex_home = home / "codex-home"
            codex_home.mkdir(mode=0o700)
            schema_path = cwd / "response-schema.json"
            _write_json(schema_path, adapters.response_schema(_SYNTHETIC_PACKET))
            env = {key: value for key, value in os.environ.items() if key in _SAFE_CHILD_ENV}
            env["HOME"] = str(home)
            env["CODEX_HOME"] = str(codex_home)
            base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
            argv = native_codex.build_closed_book_argv(
                str(probe.binary_path),
                model=model,
                effort=effort,
                response_schema_path=schema_path,
                transport_overrides=_loopback_overrides(base_url),
            )
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr, timed_out, overflow = _bounded_process_output(process)
            if overflow:
                error = "process output exceeds bound"
            returncode = process.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        error = type(exc).__name__
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    _write_new(case_root / "process_stdout.bin", stdout)
    _write_new(case_root / "process_stderr.bin", stderr)
    first = state.posts[0] if state.posts else None
    inert = (
        error is None
        and not timed_out
        and returncode == 0
        and state.body_error is None
        and len(state.posts) >= 2
        and _advertisement_matches(case, first)
        and _expected_output(case, state.tool_outputs)
    )
    capture = {
        "schema": "native-codex-handler-capture.v2",
        "scope": "local loopback fixture; no credentials or provider inference",
        "handler": case.receipt_name,
        "namespace": case.namespace,
        "name": case.name,
        "injected_envelope": case.envelope,
        "timeout_seconds": _TIMEOUT_SECONDS,
        "timed_out": timed_out,
        "returncode": returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "error": error,
        "body_error": state.body_error,
        "fresh_home": True,
        "fresh_neutral_cwd": True,
        "child_environment_keys": sorted(_SAFE_CHILD_ENV | {"HOME", "CODEX_HOME"}),
        "request_count": len(state.posts),
        "requests": state.posts,
        "response_events": state.response_events,
        "tool_outputs": state.tool_outputs,
        "stdout_sha256": _sha256(stdout),
        "stderr_sha256": _sha256(stderr),
        "inert": inert,
    }
    capture_path = case_root / "capture.json"
    _write_json(capture_path, capture)
    return {"capture": capture, "path": capture_path, "sha256": _sha256(capture_path.read_bytes())}


def _build_receipt(
    probe: native_codex._CliProbe,
    *,
    model: str,
    effort: str,
    captures: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    expected = set(native_codex._INERT_HANDLER_NAMES)
    if set(captures) != expected:
        raise ControlProbeError("control probe did not capture every required handler")
    if any(not capture.get("capture", {}).get("inert") for capture in captures.values()):
        raise ControlProbeError("control probe did not prove every handler inert")
    request_shape_sha256 = adapters.digest(native_codex._request_shape({"model": model, "effort": effort}))
    evidence = {
        name: {
            "source_kind": "local-mock-capture",
            "artifact_path": str(captures[name]["path"]),
            "artifact_sha256": captures[name]["sha256"],
            "request_shape_sha256": request_shape_sha256,
        }
        for name in sorted(expected)
    }
    return {
        "schema": "native-codex-control.v2",
        "entrypoint_sha256": probe.entrypoint_sha256,
        "native_runtime_sha256": probe.native_runtime_sha256,
        "cli_version": probe.version,
        "probe_implementation_sha256": native_codex._probe_implementation_sha256(),
        "request_shape_sha256": request_shape_sha256,
        "fresh_neutral_cwd": True,
        "fresh_home": True,
        "ignore_user_config": True,
        "ignore_rules": True,
        "ephemeral": True,
        "handler_controls": {name: "inert" for name in sorted(expected)},
        "handler_evidence": evidence,
    }


def run_probe(output: str | os.PathLike[str] | Path, *, codex_bin: str, model: str, effort: str) -> dict[str, Any]:
    """Create immutable local captures and a v2 receipt, or raise after failures."""

    if effort not in native_codex._EFFORTS:
        raise ControlProbeError("requested effort is unsupported")
    native_codex.validate_codex_config({"adapter": "codex", "model": model, "codex_bin": codex_bin})
    root = Path(output)
    if not root.is_absolute() or root.exists() or root.is_symlink():
        raise ControlProbeError("output must be a new absolute directory")
    root.mkdir(mode=0o700, parents=True)
    probe = native_codex._probe_cli(codex_bin, _TIMEOUT_SECONDS)
    captures = {
        receipt_name: _run_case(root, probe, model, effort, _HandlerCase(*case))
        for case in _HANDLERS
        for receipt_name in [case[0]]
    }
    request_shape = native_codex._request_shape({"model": model, "effort": effort})
    report = {
        "schema": "native-codex-control-report.v2",
        "scope": "local loopback fixture; no credentials or provider inference",
        "status": "passed" if all(item["capture"]["inert"] for item in captures.values()) else "failed",
        "entrypoint_sha256": probe.entrypoint_sha256,
        "native_runtime_sha256": probe.native_runtime_sha256,
        "cli_version": probe.version,
        "requested_model": model,
        "requested_effort": effort,
        "request_shape_sha256": adapters.digest(request_shape),
        "shared_invocation_controls": request_shape,
        "transport_only_additions": ["loopback model provider", "fresh empty CODEX_HOME", "synthetic schema"],
        "effective_backend_model": "unknown",
        "accepted_effort": "unknown",
        "effective_effort": "unknown",
        "max_output_tokens_effective": "unknown",
        "captures": {name: {"path": str(item["path"]), "sha256": item["sha256"]} for name, item in captures.items()},
        "limitations": [
            "Applies only to this fresh noninteractive codex exec invocation.",
            "Does not establish behavior for interactive or Plan-mode hosts.",
            "Does not establish backend model identity, accepted/effective effort, or a hard output-token limit.",
        ],
    }
    _write_json(root / "report.json", report)
    receipt = _build_receipt(probe, model=model, effort=effort, captures=captures)
    _write_json(root / native_codex.CODEX_CONTROL_FILE, receipt)
    return {"report": report, "receipt": receipt, "output": root}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new absolute private capture directory")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--effort", default="medium")
    args = parser.parse_args(argv)
    try:
        outcome = run_probe(args.output, codex_bin=args.codex_bin, model=args.model, effort=args.effort)
    except ControlProbeError as exc:
        print(f"codex control probe failed: {exc}")
        return 2
    print(adapters.canonical({"output": str(outcome["output"]), "status": outcome["report"]["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
