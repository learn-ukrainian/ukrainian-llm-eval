"""Real-process proofs for the private admission command boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from ukrainian_llm_eval.admission_command import (
    COMMAND_SPEC_SCHEMA,
    AdmissionCommandError,
    command_identity_sha256,
    invoke_admission,
    validate_command_spec,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _command_spec(script: Path, lock: Path, **changes):
    executable = Path(sys.executable).resolve()
    spec = {
        "schema": COMMAND_SPEC_SCHEMA,
        "runtime": "python-script-v1",
        "argv": [os.fspath(executable), os.fspath(script)],
        "declared_files": [
            {"path": os.fspath(executable), "byte_sha256": _sha256(executable), "role": "executable"},
            {"path": os.fspath(script), "byte_sha256": _sha256(script), "role": "script"},
            {"path": os.fspath(lock), "byte_sha256": _sha256(lock), "role": "runtime_lock"},
        ],
        "env_names": [],
        "timeout_seconds": 5,
        "stdin_max_bytes": 4096,
        "stdout_max_bytes": 4096,
        "stderr_max_bytes": 4096,
    }
    spec.update(changes)
    return spec


def _fixture(tmp_path: Path, source: str):
    script = tmp_path / "probe.py"
    script.write_text(source, encoding="utf-8")
    lock = tmp_path / "requirements.lock"
    lock.write_text("# deliberately empty runtime lock\n", encoding="utf-8")
    return script, lock


def test_validates_strict_spec_and_stable_identity(tmp_path):
    script, lock = _fixture(tmp_path, "print('ok')\n")
    spec = _command_spec(script, lock, env_names=["B_ENV", "A_ENV"])

    normalized = validate_command_spec(spec)

    assert normalized is not spec
    assert normalized["env_names"] == ["A_ENV", "B_ENV"]
    assert command_identity_sha256(spec) == command_identity_sha256(normalized)


def test_multiple_declared_dependencies_run_only_from_snapshot(tmp_path):
    script, lock = _fixture(tmp_path, "import helper_one, helper_two\nprint(helper_one.VALUE + helper_two.VALUE)\n")
    helper_one = tmp_path / "helper_one.py"
    helper_two = tmp_path / "helper_two.py"
    helper_one.write_text("VALUE = 'snap'\n", encoding="utf-8")
    helper_two.write_text("VALUE = 'shot'\n", encoding="utf-8")
    spec = _command_spec(script, lock)
    spec["declared_files"].extend(
        [
            {"path": os.fspath(helper_one), "byte_sha256": _sha256(helper_one), "role": "dependency"},
            {"path": os.fspath(helper_two), "byte_sha256": _sha256(helper_two), "role": "dependency"},
        ]
    )

    result = invoke_admission(spec, {"nonce": "dependency-proof"})

    assert result["status"] == "success"
    assert result["stdout"] == b"snapshot\n"


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "wrong"},
        {"runtime": "native"},
        {"env_names": ["PYTHONPATH"]},
        {"argv": ["python", "probe.py"]},
    ],
)
def test_rejects_unsupported_or_implicit_command_shapes(tmp_path, change):
    script, lock = _fixture(tmp_path, "print('no')\n")
    spec = _command_spec(script, lock)
    spec.update(change)
    with pytest.raises(AdmissionCommandError):
        validate_command_spec(spec)


def test_executes_private_snapshot_with_exact_environment_and_bounded_stdout(monkeypatch, tmp_path):
    script, lock = _fixture(
        tmp_path,
        """import json, os, pathlib, sys
request = json.load(sys.stdin)
result = {
    "nonce": request["nonce"],
    "allowed": os.environ.get("ADMISSION_ALLOWED"),
    "extra": os.environ.get("ADMISSION_EXTRA"),
    "cwd": os.getcwd(),
    "script": str(pathlib.Path(__file__).resolve()),
}
sys.stderr.write("private diagnostic")
sys.stdout.write(json.dumps(result, sort_keys=True))
""",
    )
    monkeypatch.setenv("ADMISSION_ALLOWED", "visible")
    monkeypatch.setenv("ADMISSION_EXTRA", "must-not-pass")
    spec = _command_spec(script, lock, env_names=["ADMISSION_ALLOWED"])

    result = invoke_admission(spec, {"nonce": "n-1"})

    assert result["status"] == "success"
    observed = json.loads(result["stdout"])
    assert observed["nonce"] == "n-1"
    assert observed["allowed"] == "visible"
    assert observed["extra"] is None
    assert Path(observed["cwd"]).name == "cwd"
    assert Path(observed["script"]).parent.name == "files"
    assert not Path(observed["cwd"]).exists()
    assert "stderr" not in result
    assert result["stderr_sha256"] == hashlib.sha256(b"private diagnostic").hexdigest()


def test_argv_is_passed_literally_without_shell_and_missing_env_fails_closed(monkeypatch, tmp_path):
    script, lock = _fixture(tmp_path, "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    literal = "$(printf SHELL_EXPANDED)"
    spec = _command_spec(script, lock)
    spec["argv"].append(literal)

    success = invoke_admission(spec, {"nonce": "n-literal"})
    monkeypatch.delenv("ADMISSION_ABSENT", raising=False)
    missing = invoke_admission(_command_spec(script, lock, env_names=["ADMISSION_ABSENT"]), {"nonce": "n-missing"})

    assert success["status"] == "success"
    assert json.loads(success["stdout"]) == [literal]
    assert missing["status"] == "environment_missing"
    assert "stdout" not in missing and "stderr" not in missing


def test_mutated_script_is_rejected_without_execution(tmp_path):
    marker = tmp_path / "ran"
    script, lock = _fixture(tmp_path, f"from pathlib import Path\nPath({os.fspath(marker)!r}).write_text('ran')\n")
    spec = _command_spec(script, lock)
    script.write_text("raise SystemExit('changed')\n", encoding="utf-8")

    result = invoke_admission(spec, {"nonce": "n-2"})

    assert result["status"] == "identity_mismatch"
    assert not marker.exists()
    assert "stdout" not in result and "stderr" not in result


def test_invalid_json_and_nonzero_output_are_never_returned(tmp_path):
    script, lock = _fixture(tmp_path, "import sys\nsys.stdout.write('RAW SECRET')\nraise SystemExit(2)\n")
    spec = _command_spec(script, lock)

    invalid = invoke_admission(spec, {1: "non-string JSON key"})
    failed = invoke_admission(spec, {"nonce": "n-2b"})

    assert invalid["status"] == "invalid_request"
    assert failed["status"] == "nonzero_exit"
    assert "stdout" not in failed and "stderr" not in failed
    assert "RAW SECRET" not in repr(failed)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_overflow_kills_process_and_returns_only_counts_and_hashes(tmp_path, stream):
    target = "sys.stdout.buffer" if stream == "stdout" else "sys.stderr.buffer"
    script, lock = _fixture(
        tmp_path,
        f"import sys, time\n{target}.write(b'SECRET' * 10000)\n{target}.flush()\ntime.sleep(30)\n",
    )
    spec = _command_spec(script, lock, stdout_max_bytes=128, stderr_max_bytes=128)

    result = invoke_admission(spec, {"nonce": "n-3"})

    assert result["status"] == "output_overflow"
    assert result[f"{stream}_byte_count"] > 128
    assert "stdout" not in result and "stderr" not in result
    assert "SECRET" not in repr(result)


def test_timeout_kills_child_process_group(tmp_path, monkeypatch):
    child_pid_file = tmp_path / "child.pid"
    script, lock = _fixture(
        tmp_path,
        """import os, subprocess, sys, time
subprocess.Popen([
    sys.executable,
    "-c",
    "import os,time; open(os.environ['CHILD_PID_FILE'],'w').write(str(os.getpid())); time.sleep(30)",
])
deadline = time.monotonic() + 2
while not os.path.exists(os.environ["CHILD_PID_FILE"]) and time.monotonic() < deadline:
    time.sleep(0.01)
time.sleep(30)
""",
    )
    monkeypatch.setenv("CHILD_PID_FILE", os.fspath(child_pid_file))
    spec = _command_spec(script, lock, env_names=["CHILD_PID_FILE"], timeout_seconds=1.0)

    result = invoke_admission(spec, {"nonce": "n-4"})

    assert result["status"] == "timeout"
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("admission timeout left its child process alive")
    assert "stdout" not in result and "stderr" not in result
