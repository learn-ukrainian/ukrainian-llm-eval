"""Run an explicitly configured admission command from verified file bytes.

This module deliberately supports one narrow runtime shape: a directly invoked
Python interpreter followed by one Python script.  The interpreter, script,
runtime lock files, and any declared dependency files are copied from already
verified bytes into a private snapshot before execution.  This closes the
hash-then-reopen race for those files.

The snapshot identity is intentionally limited to the declared files.  It does
not claim to freeze the interpreter's dynamic loader, shared libraries, standard
library, kernel, or undeclared transitive imports.  The command is a trusted
user-owned integration boundary, not a sandbox for hostile code.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

COMMAND_SPEC_SCHEMA = "ukrainian-llm-eval.admission-command.v1"
_RUNTIME = "python-script-v1"
_SPEC_FIELDS = {
    "schema",
    "runtime",
    "argv",
    "declared_files",
    "env_names",
    "timeout_seconds",
    "stdin_max_bytes",
    "stdout_max_bytes",
    "stderr_max_bytes",
}
_FILE_FIELDS = {"path", "byte_sha256", "role"}
_FILE_ROLES = {"executable", "script", "runtime_lock", "dependency"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CODE_LOADING_ENV_NAMES = {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT", "LD_PRELOAD", "LD_LIBRARY_PATH"}

# Protocol ceilings prevent a trusted but mistaken specification from turning
# the admission boundary into an unbounded local resource sink.  Each spec must
# still provide its smaller, route-appropriate limits explicitly.
_MAX_TIMEOUT_SECONDS = 300.0
_MAX_STDIN_BYTES = 4 * 1024 * 1024
_MAX_STREAM_BYTES = 16 * 1024 * 1024
_MAX_DECLARED_FILE_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_DECLARED_BYTES = 256 * 1024 * 1024


class AdmissionCommandError(ValueError):
    """A normalized validation failure safe for internal classification."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


def _reject(status: str = "invalid_spec") -> None:
    raise AdmissionCommandError(status)


def _strict_positive_int(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        _reject()
    return value


def _require_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 100:
        _reject("invalid_request")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _reject("invalid_request")
        return
    if isinstance(value, list):
        for item in value:
            _require_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            _reject("invalid_request")
        for item in value.values():
            _require_json_value(item, depth=depth + 1)
        return
    _reject("invalid_request")


def _canonical_json_bytes(value: Any) -> bytes:
    _require_json_value(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError):
        _reject("invalid_request")


def _read_verified_file(path: str, expected_sha256: str) -> bytes:
    if not hasattr(os, "O_NOFOLLOW") or os.name != "posix":
        _reject("unsupported_runtime")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        _reject("identity_mismatch")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_DECLARED_FILE_BYTES:
            _reject("identity_mismatch")
        chunks: list[bytes] = []
        remaining = _MAX_DECLARED_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0 and os.read(descriptor, 1):
            _reject("identity_mismatch")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            _reject("identity_mismatch")
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            _reject("identity_mismatch")
        return payload
    finally:
        os.close(descriptor)


def _validate_shape(spec: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(spec, Mapping) or set(spec) != _SPEC_FIELDS:
        _reject()
    if spec["schema"] != COMMAND_SPEC_SCHEMA or spec["runtime"] != _RUNTIME:
        _reject("unsupported_runtime")

    argv = spec["argv"]
    if not isinstance(argv, list) or len(argv) < 2 or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
        _reject()
    if not os.path.isabs(argv[0]) or not os.path.isabs(argv[1]):
        _reject()
    # Python -c/-m/stdin execution and implicit relative code loading are not
    # supported.  Extra absolute operands must also be declared and snapshotted.
    if any(("/" in arg or os.sep in arg) and not os.path.isabs(arg) for arg in argv[2:]):
        _reject("unsupported_runtime")

    files = spec["declared_files"]
    if not isinstance(files, list) or not files:
        _reject()
    normalized_files: list[dict[str, str]] = []
    paths: set[str] = set()
    basenames: set[str] = set()
    role_counts = {role: 0 for role in _FILE_ROLES}
    for entry in files:
        if not isinstance(entry, Mapping) or set(entry) != _FILE_FIELDS:
            _reject()
        path = entry["path"]
        byte_sha256 = entry["byte_sha256"]
        role = entry["role"]
        if (
            not isinstance(path, str)
            or not os.path.isabs(path)
            or not isinstance(byte_sha256, str)
            or _SHA256_RE.fullmatch(byte_sha256) is None
            or not isinstance(role, str)
            or role not in _FILE_ROLES
        ):
            _reject()
        basename = os.path.basename(path)
        if not basename or path in paths or basename in basenames:
            _reject()
        try:
            path_stat = os.lstat(path)
        except OSError:
            _reject("identity_mismatch")
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            _reject("identity_mismatch")
        if role == "executable" and path_stat.st_mode & 0o111 == 0:
            _reject("identity_mismatch")
        paths.add(path)
        basenames.add(basename)
        role_counts[role] += 1
        normalized_files.append({"path": path, "byte_sha256": byte_sha256, "role": role})

    if role_counts["executable"] != 1 or role_counts["script"] != 1 or role_counts["runtime_lock"] < 1:
        _reject()
    by_role = {entry["role"]: entry["path"] for entry in normalized_files if entry["role"] in {"executable", "script"}}
    if argv[0] != by_role["executable"] or argv[1] != by_role["script"]:
        _reject()
    declared_paths = {entry["path"] for entry in normalized_files}
    if any(os.path.isabs(arg) and arg not in declared_paths for arg in argv[2:]):
        _reject()

    env_names = spec["env_names"]
    if not isinstance(env_names, list) or any(not isinstance(name, str) or _ENV_NAME_RE.fullmatch(name) is None for name in env_names):
        _reject()
    if len(set(env_names)) != len(env_names):
        _reject()
    for name in env_names:
        if name in _CODE_LOADING_ENV_NAMES or name.startswith("DYLD_"):
            _reject("unsupported_runtime")

    timeout = spec["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        _reject()
    timeout = float(timeout)
    if not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
        _reject()

    normalized_files.sort(key=lambda item: item["path"])
    return {
        "schema": COMMAND_SPEC_SCHEMA,
        "runtime": _RUNTIME,
        "argv": list(argv),
        "declared_files": normalized_files,
        "env_names": sorted(env_names),
        "timeout_seconds": timeout,
        "stdin_max_bytes": _strict_positive_int(spec["stdin_max_bytes"], _MAX_STDIN_BYTES),
        "stdout_max_bytes": _strict_positive_int(spec["stdout_max_bytes"], _MAX_STREAM_BYTES),
        "stderr_max_bytes": _strict_positive_int(spec["stderr_max_bytes"], _MAX_STREAM_BYTES),
    }


def _load_declared_files(normalized: Mapping[str, object]) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    total = 0
    for entry in normalized["declared_files"]:  # type: ignore[union-attr]
        payload = _read_verified_file(entry["path"], entry["byte_sha256"])
        total += len(payload)
        if total > _MAX_TOTAL_DECLARED_BYTES:
            _reject("identity_mismatch")
        payloads[entry["path"]] = payload
    return payloads


def validate_command_spec(spec: Mapping[str, object]) -> dict[str, object]:
    """Return a strict normalized copy after verifying every declared file.

    No executable or script is discovered from benchmark data.  The caller must
    explicitly supply the complete command specification and byte hashes.
    """

    normalized = _validate_shape(spec)
    _load_declared_files(normalized)
    return normalized


def _identity_from_normalized(normalized: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(normalized)).hexdigest()


def command_identity_sha256(spec: Mapping[str, object]) -> str:
    """Hash the normalized spec after verifying the exact declared file bytes.

    Environment values are deliberately absent: only explicitly requested names
    are bound.  Declared file digests bind the verified bytes, while the module
    docstring states the transitive runtime coverage limit.
    """

    normalized = _validate_shape(spec)
    _load_declared_files(normalized)
    return _identity_from_normalized(normalized)


class _Capture:
    def __init__(self, stream: BinaryIO, limit: int, overflow: threading.Event):
        self.stream = stream
        self.limit = limit
        self.overflow = overflow
        self.count = 0
        self.digest = hashlib.sha256()
        self.buffer = bytearray()
        self.failed = False

    def read(self) -> None:
        try:
            while True:
                chunk = os.read(self.stream.fileno(), 64 * 1024)
                if not chunk:
                    return
                self.count += len(chunk)
                self.digest.update(chunk)
                if self.count <= self.limit:
                    self.buffer.extend(chunk)
                else:
                    self.buffer.clear()
                    self.overflow.set()
        except OSError:
            self.failed = True
            self.overflow.set()
        finally:
            try:
                self.stream.close()
            except OSError:
                self.failed = True


def _write_stdin(stream: BinaryIO, payload: bytes) -> None:
    try:
        stream.write(payload)
        stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def _result(
    status: str,
    stdout_capture: _Capture | None = None,
    stderr_capture: _Capture | None = None,
    *,
    command_identity: str | None = None,
    stdout: bytes | None = None,
) -> dict[str, object]:
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    result: dict[str, object] = {
        "status": status,
        "stdout_byte_count": 0 if stdout_capture is None else stdout_capture.count,
        "stderr_byte_count": 0 if stderr_capture is None else stderr_capture.count,
        "stdout_sha256": empty_sha256 if stdout_capture is None else stdout_capture.digest.hexdigest(),
        "stderr_sha256": empty_sha256 if stderr_capture is None else stderr_capture.digest.hexdigest(),
    }
    if command_identity is not None:
        result["command_identity_sha256"] = command_identity
    if status == "success" and stdout is not None:
        result["stdout"] = stdout
    return result


def _snapshot_command(
    normalized: Mapping[str, object], payloads: Mapping[str, bytes], snapshot_dir: Path
) -> tuple[list[str], Path]:
    files_dir = snapshot_dir / "files"
    cwd = snapshot_dir / "cwd"
    files_dir.mkdir(mode=0o700)
    cwd.mkdir(mode=0o700)
    path_map: dict[str, str] = {}
    roles = {entry["path"]: entry["role"] for entry in normalized["declared_files"]}  # type: ignore[union-attr]
    for original_path, payload in payloads.items():
        destination = files_dir / os.path.basename(original_path)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        if roles[original_path] == "executable":
            os.chmod(destination, 0o700)
        path_map[original_path] = os.fspath(destination)
    argv = [path_map.get(argument, argument) for argument in normalized["argv"]]  # type: ignore[union-attr]
    return argv, cwd


def invoke_admission(spec: Mapping[str, object], request: Mapping[str, object]) -> dict[str, object]:
    """Invoke one verified command and return raw stdout only on success.

    Failures contain normalized status, byte counts, and SHA-256 hashes only.
    Raw stderr and failed stdout are never returned, logged, or persisted here.
    """

    try:
        normalized = _validate_shape(spec)
        payloads = _load_declared_files(normalized)
        command_identity = _identity_from_normalized(normalized)
    except AdmissionCommandError as exc:
        return _result(exc.status)

    if not isinstance(request, Mapping):
        return _result("invalid_request", command_identity=command_identity)
    try:
        stdin_payload = _canonical_json_bytes(dict(request)) + b"\n"
    except AdmissionCommandError:
        return _result("invalid_request", command_identity=command_identity)
    if len(stdin_payload) > normalized["stdin_max_bytes"]:
        return _result("invalid_request", command_identity=command_identity)

    env_names = normalized["env_names"]
    if any(name not in os.environ for name in env_names):
        return _result("environment_missing", command_identity=command_identity)
    environment = {name: os.environ[name] for name in env_names}

    stdout_capture: _Capture | None = None
    stderr_capture: _Capture | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="ukrainian-llm-eval-admission-") as temp_name:
            snapshot_dir = Path(temp_name)
            os.chmod(snapshot_dir, 0o700)
            argv, cwd = _snapshot_command(normalized, payloads, snapshot_dir)
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    start_new_session=True,
                )
            except OSError:
                return _result("spawn_error", command_identity=command_identity)

            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            overflow = threading.Event()
            stdout_capture = _Capture(process.stdout, normalized["stdout_max_bytes"], overflow)
            stderr_capture = _Capture(process.stderr, normalized["stderr_max_bytes"], overflow)
            threads = [
                threading.Thread(target=stdout_capture.read, daemon=True),
                threading.Thread(target=stderr_capture.read, daemon=True),
                threading.Thread(target=_write_stdin, args=(process.stdin, stdin_payload), daemon=True),
            ]
            for thread in threads:
                thread.start()

            deadline = time.monotonic() + normalized["timeout_seconds"]
            terminal_status: str | None = None
            return_code: int | None = None
            while return_code is None:
                if overflow.is_set():
                    terminal_status = "output_overflow"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminal_status = "timeout"
                    break
                try:
                    return_code = process.wait(timeout=min(remaining, 0.02))
                except subprocess.TimeoutExpired:
                    continue

            # Kill the whole session even after the direct child exits, so a
            # plugin cannot leave inherited-pipe grandchildren behind.
            _kill_process_group(process)
            if return_code is None:
                try:
                    return_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    terminal_status = "cleanup_failed"
            for thread in threads:
                thread.join(timeout=5)
            if any(thread.is_alive() for thread in threads) or stdout_capture.failed or stderr_capture.failed:
                terminal_status = "cleanup_failed"
            elif overflow.is_set():
                terminal_status = "output_overflow"

            if terminal_status is not None:
                return _result(
                    terminal_status,
                    stdout_capture,
                    stderr_capture,
                    command_identity=command_identity,
                )
            if return_code != 0:
                return _result(
                    "nonzero_exit",
                    stdout_capture,
                    stderr_capture,
                    command_identity=command_identity,
                )
            return _result(
                "success",
                stdout_capture,
                stderr_capture,
                command_identity=command_identity,
                stdout=bytes(stdout_capture.buffer),
            )
    except OSError:
        if process is not None:
            _kill_process_group(process)
        return _result(
            "execution_error",
            stdout_capture,
            stderr_capture,
            command_identity=command_identity,
        )
    except BaseException:
        if process is not None:
            _kill_process_group(process)
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise
