"""Real-process proofs for the private admission command boundary."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

from ukrainian_llm_eval import admission_command
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


@pytest.mark.parametrize("replacement", ["missing", "directory"])
def test_declared_script_must_remain_a_readable_regular_file(tmp_path, replacement):
    marker = tmp_path / "ran"
    script, lock = _fixture(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    spec = _command_spec(script, lock)
    validate_command_spec(spec)
    script.unlink()
    if replacement == "directory":
        script.mkdir()

    with pytest.raises(AdmissionCommandError, match="identity_mismatch"):
        admission_command._read_verified_file(str(script), spec["declared_files"][1]["byte_sha256"])
    result = invoke_admission(spec, {})

    assert result["status"] == "identity_mismatch"
    assert result["stdout_byte_count"] == result["stderr_byte_count"] == 0
    assert "stdout" not in result and "stderr" not in result
    assert not marker.exists()


@pytest.mark.parametrize("payload", [[], {"private": "SECRET" * 100}])
def test_invalid_or_oversized_request_is_rejected_before_execution(tmp_path, payload):
    marker = tmp_path / "ran"
    script, lock = _fixture(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    spec = _command_spec(script, lock, stdin_max_bytes=32)

    result = invoke_admission(spec, payload)

    assert result["status"] == "invalid_request"
    assert result["command_identity_sha256"] == command_identity_sha256(spec)
    assert result["stdout_byte_count"] == result["stderr_byte_count"] == 0
    assert "stdout" not in result and "stderr" not in result
    assert "SECRET" not in repr(result)
    assert not marker.exists()


def test_invalid_executable_bytes_return_private_bounded_spawn_error(tmp_path):
    marker = tmp_path / "ran"
    script, lock = _fixture(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    spec = _command_spec(script, lock)
    executable = tmp_path / "invalid-python"
    executable.write_bytes(b"SECRET invalid executable\x00")
    executable.chmod(0o700)
    spec["argv"][0] = str(executable)
    spec["declared_files"][0].update(path=str(executable), byte_sha256=_sha256(executable))

    result = invoke_admission(spec, {})

    assert result["status"] == "spawn_error"
    assert result["command_identity_sha256"] == command_identity_sha256(spec)
    assert result["stdout_byte_count"] == result["stderr_byte_count"] == 0
    assert "stdout" not in result and "stderr" not in result
    assert "SECRET" not in repr(result)
    assert not marker.exists()


def test_copied_runtime_preserves_bytes_base_prefix_extensions_and_empty_environment(monkeypatch, tmp_path):
    script, lock = _fixture(tmp_path, """import hashlib, json, math, os, pathlib, site, sys
request = json.load(sys.stdin)
root = pathlib.Path(sys.executable).parent.parent
print(json.dumps({
    'nonce': request['nonce'], 'sqrt': math.sqrt(9), 'environment': dict(os.environ),
    'base_prefix': sys.base_prefix, 'base_exec_prefix': sys.base_exec_prefix,
    'user_site': site.ENABLE_USER_SITE, 'paths': sys.path,
    'executable_sha256': hashlib.sha256(pathlib.Path(sys.executable).read_bytes()).hexdigest(),
    'script_sha256': hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
    'config': (root / 'pyvenv.cfg').read_text(encoding='utf-8'),
    'config_mode': (root / 'pyvenv.cfg').stat().st_mode & 0o777,
}))
""")
    monkeypatch.setenv("PYTHONHOME", "/absent-runtime")
    monkeypatch.setenv("PYTHONPATH", os.fspath(tmp_path / "ambient"))
    monkeypatch.setenv("LD_PRELOAD", "/absent-loader")
    popen = admission_command.subprocess.Popen
    environments = []

    def observe_spawn(*args, **kwargs):
        environments.append(kwargs["env"])
        return popen(*args, **kwargs)

    monkeypatch.setattr(admission_command.subprocess, "Popen", observe_spawn)
    spec = _command_spec(script, lock)
    expected_identity = hashlib.sha256(admission_command._canonical_json_bytes(
        admission_command._validate_shape(spec))).hexdigest()
    result = invoke_admission(spec, {"nonce": "runtime-binding"})
    assert result["status"] == "success"
    assert result["command_identity_sha256"] == expected_identity == command_identity_sha256(spec)
    observed = json.loads(result["stdout"])
    assert observed["nonce"] == "runtime-binding"
    assert observed["sqrt"] == 3
    assert environments == [{}]
    # CPython can coerce the empty C locale by setting LC_CTYPE itself.
    assert set(observed["environment"]) <= {"LC_CTYPE"}
    assert observed["base_prefix"] == sys.base_prefix
    assert observed["base_exec_prefix"] == sys.base_exec_prefix
    assert observed["user_site"] is False
    assert not any("site-packages" in path or "dist-packages" in path for path in observed["paths"])
    assert observed["executable_sha256"] == spec["declared_files"][0]["byte_sha256"]
    assert observed["script_sha256"] == _sha256(script)
    assert observed["config_mode"] == 0o600
    assert observed["config"] == (
        f"home = {Path(spec['argv'][0]).resolve().parent}\ninclude-system-site-packages = false\n")


@pytest.mark.parametrize("basename", ["pyvenv.cfg", "executable_pth"])
def test_declared_config_collisions_reject_before_spawn(monkeypatch, tmp_path, basename):
    script, lock = _fixture(tmp_path, "print('must not run')\n")
    spec = _command_spec(script, lock)
    if basename == "executable_pth":
        basename = Path(spec["argv"][0]).name + "._pth"
    dependency = tmp_path / basename
    dependency.write_text("include-system-site-packages = true\n", encoding="utf-8")
    spec["declared_files"].append(
        {"path": os.fspath(dependency), "byte_sha256": _sha256(dependency), "role": "dependency"})
    monkeypatch.setattr(admission_command.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned collision"))
    result = invoke_admission(spec, {})
    assert result["status"] == "unsupported_runtime"
    assert result["stdout_byte_count"] == result["stderr_byte_count"] == 0
    with pytest.raises(AdmissionCommandError, match="unsupported_runtime"):
        validate_command_spec(spec)


@pytest.mark.parametrize("basename", ["PYVENV.CFG", "executable_pth"])
def test_casefold_collisions_on_insensitive_destination_reject(monkeypatch, tmp_path, basename):
    script, lock = _fixture(tmp_path, "print('must not run')\n")
    spec = _command_spec(script, lock)
    if basename == "executable_pth":
        basename = (Path(spec["argv"][0]).name + "._pth").upper()
    dependency = tmp_path / basename
    dependency.write_text("unsafe config\n", encoding="utf-8")
    spec["declared_files"].append(
        {"path": os.fspath(dependency), "byte_sha256": _sha256(dependency), "role": "dependency"})
    original_exists = Path.exists

    def case_insensitive_exists(path):
        if path.name == "PYVENV.CFG" and path.parent.name.startswith("ukrainian-llm-eval-admission-"):
            return original_exists(path.with_name("pyvenv.cfg"))
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", case_insensitive_exists)
    monkeypatch.setattr(admission_command.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned collision"))
    assert invoke_admission(spec, {})["status"] == "unsupported_runtime"


def _local_executable_spec(tmp_path, home):
    home.mkdir(parents=True)
    executable = home / Path(sys.executable).resolve().name
    shutil.copyfile(Path(sys.executable).resolve(), executable)
    executable.chmod(0o700)
    script, lock = _fixture(tmp_path, "print('captured')\n")
    spec = _command_spec(script, lock)
    spec["argv"][0] = os.fspath(executable)
    spec["declared_files"][0].update(path=os.fspath(executable), byte_sha256=_sha256(executable))
    return spec


@pytest.mark.parametrize("suffix", ["\n", "\r", " ", "\t", "\x0b", "\x0c", "\x85", "\u2028", "\udcff"])
def test_unrepresentable_runtime_home_rejects_before_spawn(monkeypatch, tmp_path, suffix):
    spec = _local_executable_spec(tmp_path, tmp_path / ("runtime" + suffix))
    monkeypatch.setattr(admission_command.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned unsafe home"))
    assert invoke_admission(spec, {})["status"] == "unsupported_runtime"


def test_internal_unicode_line_separator_fails_config_roundtrip(monkeypatch, tmp_path):
    spec = _local_executable_spec(tmp_path, tmp_path / "runtime\u2028other")
    monkeypatch.setattr(admission_command.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned ambiguous home"))
    assert invoke_admission(spec, {})["status"] == "unsupported_runtime"


@pytest.mark.parametrize("location", ["neighbor", "parent", "pth", "broken_symlink"])
def test_original_runtime_sidecars_are_unsupported(monkeypatch, tmp_path, location):
    spec = _local_executable_spec(tmp_path, tmp_path / "runtime" / "bin")
    executable = Path(spec["argv"][0])
    sidecar = executable.with_name("pyvenv.cfg")
    if location == "parent":
        sidecar = executable.parent.parent / "pyvenv.cfg"
    elif location == "pth":
        sidecar = Path(os.fspath(executable) + "._pth")
    if location == "broken_symlink":
        sidecar.symlink_to(tmp_path / "missing-config")
    else:
        sidecar.write_text("home = /other\n", encoding="utf-8")
    monkeypatch.setattr(admission_command.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned sidecar"))
    assert invoke_admission(spec, {})["status"] == "unsupported_runtime"
    with pytest.raises(AdmissionCommandError, match="unsupported_runtime"):
        command_identity_sha256(spec)


def test_symlinked_intermediate_directory_uses_verified_resolved_home(tmp_path):
    script, lock = _fixture(tmp_path, "import json, math, sys\nprint(json.dumps([sys.base_prefix, math.sqrt(9)]))\n")
    spec = _command_spec(script, lock)
    alias = tmp_path / "runtime-alias"
    alias.symlink_to(Path(spec["argv"][0]).parent, target_is_directory=True)
    aliased_executable = os.fspath(alias / Path(spec["argv"][0]).name)
    spec["argv"][0] = aliased_executable
    spec["declared_files"][0]["path"] = aliased_executable
    assert validate_command_spec(spec)["argv"][0] == aliased_executable
    result = invoke_admission(spec, {})
    assert result["status"] == "success"
    assert json.loads(result["stdout"]) == [sys.base_prefix, 3]


def test_intermediate_symlink_retarget_after_capture_rejects_inode_mismatch(monkeypatch, tmp_path):
    spec = _local_executable_spec(tmp_path, tmp_path / "original")
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    executable = Path(spec["argv"][0])
    shutil.copyfile(executable, alternate / executable.name)
    alias = tmp_path / "alias"
    alias.symlink_to(executable.parent, target_is_directory=True)
    spec["argv"][0] = spec["declared_files"][0]["path"] = os.fspath(alias / executable.name)
    load = admission_command._load_declared_files

    def retarget_after_capture(normalized):
        captured = load(normalized)
        alias.unlink()
        alias.symlink_to(alternate, target_is_directory=True)
        return captured

    monkeypatch.setattr(admission_command, "_load_declared_files", retarget_after_capture)
    monkeypatch.setattr(admission_command.subprocess, "Popen", lambda *_a, **_k: pytest.fail("spawned rebound inode"))
    assert invoke_admission(spec, {})["status"] == "identity_mismatch"


def test_script_mutation_after_capture_executes_verified_bytes(monkeypatch, tmp_path):
    script, lock = _fixture(tmp_path, "print('captured')\n")
    spec = _command_spec(script, lock)
    load = admission_command._load_declared_files

    def mutate_after_capture(normalized):
        captured = load(normalized)
        script.write_text("raise SystemExit('reopened')\n", encoding="utf-8")
        return captured

    monkeypatch.setattr(admission_command, "_load_declared_files", mutate_after_capture)
    result = invoke_admission(spec, {})
    assert result["status"] == "success"
    assert result["stdout"] == b"captured\n"


@pytest.mark.parametrize("existing", ["file", "symlink"])
def test_snapshot_config_is_exclusive_and_never_follows_symlinks(tmp_path, existing):
    script, lock = _fixture(tmp_path, "print('captured')\n")
    normalized = admission_command._validate_shape(_command_spec(script, lock))
    payloads, verified_stat = admission_command._load_declared_files(normalized)
    root = tmp_path / "snapshot"
    root.mkdir()
    target = tmp_path / "do-not-overwrite"
    target.write_bytes(b"untouched")
    config = root / "pyvenv.cfg"
    if existing == "symlink":
        config.symlink_to(target)
    else:
        config.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        admission_command._snapshot_command(normalized, payloads, root, verified_stat)
    assert target.read_bytes() == b"untouched"
    assert not (root / "files").exists()


def test_generated_config_open_uses_exclusive_nofollow_and_private_mode(monkeypatch, tmp_path):
    script, lock = _fixture(tmp_path, "print('captured')\n")
    spec = _command_spec(script, lock)
    original_open = os.open
    config_opens = []

    def observe_open(path, flags, mode=0o777, **kwargs):
        if Path(path).name == "pyvenv.cfg":
            config_opens.append((flags, mode))
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", observe_open)
    assert invoke_admission(spec, {})["status"] == "success"
    assert len(config_opens) == 1
    flags, mode = config_opens[0]
    assert flags & os.O_EXCL and flags & os.O_NOFOLLOW and flags & os.O_CREAT
    assert stat.S_IMODE(mode) == 0o600


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
