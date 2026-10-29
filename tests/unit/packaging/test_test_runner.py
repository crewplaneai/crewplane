from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import signal
import subprocess
import sys
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import run_tests, test_process
from tests.helpers.platforms import requires_posix


@requires_posix
@pytest.mark.parametrize("provider_state", ["reparented", "reused", "exited"])
def test_shutdown_cleans_only_surviving_descendants(
    provider_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = "Sun Oct 11 10:00:00 2026"
    later = "Sun Oct 11 10:00:01 2026"
    table = Mock(
        return_value=f"101 100 {started}\n102 101 {started}\n999 500 {started}\n"
    )
    monkeypatch.setattr(test_process.subprocess, "check_output", table)
    kill = Mock()
    monkeypatch.setattr(test_process.os, "kill", kill)

    def signal_group(pid: int, number: int) -> None:
        assert pid == 100
        if number == 0:
            raise ProcessLookupError

    group_signal = Mock(side_effect=signal_group)
    monkeypatch.setattr(test_process.os, "killpg", group_signal)
    process = Mock(pid=100)
    owned = test_process.OwnedProcess(process)
    owned.signal_shutdown()
    group_signal.assert_called_once_with(100, signal.SIGINT)

    table.return_value = f"101 500 {later}\n999 500 {started}\n"
    if provider_state != "exited":
        provider_start = started if provider_state == "reparented" else later
        table.return_value += f"102 1 {provider_start}\n"
    owned.close(timeout=0.3)

    if provider_state == "reparented":
        kill.assert_called_once_with(102, signal.SIGKILL)
        group_signal.assert_called_with(100, signal.SIGKILL)
    else:
        kill.assert_not_called()
    process.wait.assert_called_once_with(timeout=0.3)


@requires_posix
@pytest.mark.parametrize("child_running", [False, True])
def test_subreaper_waits_for_children_missing_from_process_snapshot(
    child_running: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = test_process.OwnedProcess(Mock(pid=100), subreaper=True)
    owned.process.poll.return_value = 7
    monkeypatch.setattr(owned, "adopted_children", Mock(return_value=[]))
    monkeypatch.setattr(os, "killpg", Mock(side_effect=ProcessLookupError))
    wait = Mock(side_effect=[(101, 0), (0, 0) if child_running else ChildProcessError])
    monkeypatch.setattr(os, "waitpid", wait)

    assert owned.has_children() is child_running


@requires_posix
def test_forced_shutdown_kills_pytest_group_if_process_inspection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = test_process.OwnedProcess(Mock(pid=100))
    owned.descendants[102] = "Sun Oct 11 10:00:00 2026"
    monkeypatch.setattr(
        test_process.subprocess,
        "check_output",
        Mock(side_effect=subprocess.TimeoutExpired("ps", 1)),
    )
    kill_group = Mock()
    monkeypatch.setattr(test_process.os, "killpg", kill_group)

    with pytest.raises(subprocess.TimeoutExpired):
        owned.kill()

    kill_group.assert_called_once_with(100, signal.SIGKILL)


@requires_posix
@pytest.mark.parametrize("interrupted", [False, True], ids=["timeout", "interrupt"])
def test_short_shutdown_grace_allows_slow_process_reaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    clock = Mock(return_value=0.0)
    process = Mock(pid=100, returncode=None)
    signals: list[int] = []

    def sleep(seconds: float) -> None:
        clock.return_value += seconds

    def signal_group(pid: int, number: int) -> None:
        assert pid == process.pid
        if process.returncode is not None:
            raise ProcessLookupError
        signals.append(number)

    def wait_for_exit(timeout: float) -> int:
        assert signal.SIGKILL in signals
        if timeout < 1:
            raise subprocess.TimeoutExpired("pytest", timeout)
        process.returncode = -signal.SIGKILL
        return process.returncode

    process.poll = lambda: process.returncode
    process.wait = wait_for_exit
    monkeypatch.setattr(test_process.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(test_process, "child_subreaper", partial(nullcontext, False))
    monkeypatch.setattr(
        test_process,
        "shutdown_signals",
        partial(nullcontext, [signal.SIGINT] if interrupted else []),
    )
    monkeypatch.setattr(test_process, "posix_processes", dict)
    monkeypatch.setattr(test_process.os, "killpg", signal_group)
    monkeypatch.setattr(test_process.time, "monotonic", clock)
    monkeypatch.setattr(test_process.time, "sleep", sleep)
    log = tmp_path / "pytest.log"

    outcome = test_process.run_logged(
        ["pytest"],
        tmp_path,
        {},
        log,
        timeout=None if interrupted else 0.1,
        shutdown_timeout=0.3,
    )

    assert outcome.timed_out is not interrupted
    assert outcome.interrupted is interrupted
    assert outcome.forced
    assert outcome.returncode == -signal.SIGKILL
    assert signal.SIGINT in signals
    assert not log.with_suffix(".active-pid").exists()


@pytest.mark.parametrize(
    ("profile", "coverage", "shuffle_seed", "required_git"),
    [
        pytest.param("local", True, None, False, id="local"),
        pytest.param("pr-313", True, 1729, True, id="pr-313"),
        pytest.param("pr-314", True, None, True, id="pr-314"),
        pytest.param("testpypi", True, None, True, id="testpypi"),
        pytest.param("windows", False, None, True, id="windows"),
        pytest.param("nightly", False, None, True, id="nightly"),
        pytest.param("nightly-shuffle", False, 38066756094, True, id="nightly-shuffle"),
        pytest.param("race", False, None, True, id="race"),
    ],
)
def test_profiles_preserve_selection_and_explicit_plugins(
    profile: str,
    coverage: bool,
    shuffle_seed: int | None,
    required_git: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CREWPLANE_REQUIRED_GIT", raising=False)
    monkeypatch.setenv("CREWPLANE_RANDOM_SEED", "38066756094")
    args = run_tests.parse_args(["--profile", profile, "--hash-seed", "42"])
    validated_local = (
        profile == "local"
        and sys.platform == "linux"
        and sys.version_info[:2] in {(3, 13), (3, 14)}
    )
    assert args.workers == (2 if validated_local else 0)
    assert args.coverage is coverage
    assert args.shuffle_seed == shuffle_seed
    assert (args.statement_floor, args.branch_floor) == (96, 90)
    command = run_tests.pytest_command(args, tmp_path)
    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert args.selection == []
    assert args.marker_expression == ("race" if profile == "race" else "")
    assert command.count("-m") == (2 if profile == "race" else 1)
    if profile == "race":
        assert command[-2:] == ["-m", "race"]
    assert ("pytest_cov" in command) is args.coverage
    assert ("randomly" in command) is (args.shuffle_seed is not None)
    assert ("xdist.plugin" in command) is bool(args.workers)
    environment = run_tests.environment_for(args, tmp_path, tmp_path / "environment")
    assert environment["PYTHONHASHSEED"] == "42"
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert (environment.get("CREWPLANE_REQUIRED_GIT") == "1") is required_git


@pytest.mark.parametrize("workers", [0, 2, 3])
def test_workers_override_uses_standard_scheduler(workers, tmp_path) -> None:
    args = run_tests.parse_args(
        ["--workers", str(workers), "--coverage", "off", "tests/unit/core"]
    )
    command = run_tests.pytest_command(args, tmp_path)
    if workers:
        start = command.index("xdist.plugin") - 1
        assert command[start : start + 6] == [
            "-p",
            "xdist.plugin",
            "-n",
            str(workers),
            "--dist=load",
            "--max-worker-restart=0",
        ]
    else:
        assert "-n" not in command and "xdist.plugin" not in command
        assert not any(arg.startswith(("--dist", "--max-worker")) for arg in command)
    assert command[-1] == "tests/unit/core"
    assert "pytest_cov" not in command


@pytest.mark.parametrize("profile", ["local", "race"])
def test_marker_expression_override_preserves_path_and_keyword_filters(
    profile: str, tmp_path: Path
) -> None:
    args = run_tests.parse_args(
        ["--profile", profile, "-m", "race and not slow", "-k", "cleanup", "tests/unit"]
    )

    command = run_tests.pytest_command(args, tmp_path)

    assert command[-5:] == ["-k", "cleanup", "-m", "race and not slow", "tests/unit"]
    assert args.marker_expression == "race and not slow"


@pytest.mark.parametrize(
    "value", ["-1", "4294967296", "38066756094", "", "invalid", "1.0"]
)
def test_invalid_hash_seeds_fail_before_launch(value) -> None:
    with pytest.raises(SystemExit):
        run_tests.parse_args(["--hash-seed", value])


@pytest.mark.parametrize("value", [None, "random", "0", "4294967295"])
def test_environment_hash_seed_is_numeric_and_separate_from_shuffle(
    value, monkeypatch
) -> None:
    if value is None:
        monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    else:
        monkeypatch.setenv("PYTHONHASHSEED", value)
    args = run_tests.parse_args(
        ["--profile", "nightly-shuffle", "--shuffle-seed", "38066756094"]
    )
    assert 0 <= args.hash_seed <= 2**32 - 1
    assert args.shuffle_seed == 38066756094
    if value not in (None, "random"):
        assert args.hash_seed == int(value)


def test_invalid_environment_hash_seed_fails(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONHASHSEED", "38066756094")
    with pytest.raises(SystemExit):
        run_tests.parse_args([])


@pytest.mark.parametrize(
    "marker_expression", ["race and not slow", ""], ids=["filtered", "unfiltered"]
)
def test_reproduction_preserves_interpreter_seeds_and_quoted_selection(
    tmp_path: Path,
    marker_expression: str,
) -> None:
    args = run_tests.parse_args(
        [
            "--profile",
            "race",
            "--hash-seed",
            "0",
            "--shuffle-seed",
            "38066756094",
            "-m",
            marker_expression,
            "a test.py::test_case[it's $literal]",
        ]
    )
    command = run_tests.reproduction_command(args, tmp_path, windows=False)
    reproduced = shlex.split(command.split(" && ", 1)[1])
    assert reproduced[0] == sys.executable
    assert reproduced[-1] == args.selection[0]
    assert reproduced[reproduced.index("--hash-seed") + 1] == "0"
    assert reproduced[reproduced.index("-m") + 1] == marker_expression
    assert run_tests.parse_args(reproduced[2:]).marker_expression == marker_expression
    windows = run_tests.reproduction_command(args, tmp_path, windows=True)
    assert "Set-Location -LiteralPath '" in windows
    assert "it''s $literal" in windows
    assert f"'-m' '{marker_expression}'" in windows
    assert "; & '" in windows


def test_launch_environment_does_not_inherit_coverage_or_pytest_overrides(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("COVERAGE_PROCESS_CONFIG", "stale config")
    monkeypatch.setenv("COVERAGE_FILE", "stale data")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--cov-append -n auto")
    args = run_tests.parse_args(["--coverage", "off"])
    environment = run_tests.environment_for(args, tmp_path, tmp_path / "environment")
    assert not any(key.startswith("COVERAGE_") for key in environment)
    assert "PYTEST_ADDOPTS" not in environment
    args.coverage = True
    environment = run_tests.environment_for(args, tmp_path, tmp_path / "environment")
    assert environment["COVERAGE_FILE"] == str(tmp_path / ".coverage")
    assert Path(environment["COVERAGE_RCFILE"]).is_absolute()


def test_missing_coverage_data_cannot_reuse_stale_report(tmp_path) -> None:
    (tmp_path / "coverage.json").write_text('{"totals": {"percent_covered": 100}}')
    result = run_tests.report_coverage(argparse.Namespace(), tmp_path, {})
    assert result[0]["returncode"] == 1
    assert "no combined coverage data" in result[0]["error"]


def test_profiles_do_not_install_dependencies(monkeypatch) -> None:
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    args = run_tests.parse_args(["--profile", "pr-313"])
    assert args.shuffle_seed == 1729
    assert args.workers == 0
    assert args.coverage is True
    assert os.path.isabs(run_tests.ROOT)


def test_invalid_environment_shuffle_seed_is_a_usage_error(monkeypatch) -> None:
    monkeypatch.setenv("CREWPLANE_RANDOM_SEED", "invalid")
    with pytest.raises(SystemExit) as error:
        run_tests.parse_args(["--profile", "nightly-shuffle"])
    assert error.value.code == 2


def test_failed_json_report_cannot_reuse_previous_report(tmp_path, monkeypatch) -> None:
    from scripts.test_process import ProcessOutcome

    (tmp_path / ".coverage").write_bytes(b"owned data")
    (tmp_path / "coverage.json").write_text('{"totals": {"percent_covered": 100}}')
    commands = []

    def fail_reporting(command, *args, **kwargs):
        del args, kwargs
        commands.append(command)
        return ProcessOutcome(7, 0)

    monkeypatch.setattr(run_tests, "run_logged", fail_reporting)
    result = run_tests.report_coverage(run_tests.parse_args([]), tmp_path, {})
    assert not (tmp_path / "coverage.json").exists()
    assert len(commands) == 2
    assert all(outcome["returncode"] != 0 for outcome in result)
    assert result[-1]["error"] == "Current invocation has no fresh coverage JSON"


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [
        pytest.param("utf-8", b"failure: \xe2\x98\x83\n", id="unicode-console"),
        pytest.param("cp1252", b"failure: \\u2603\n", id="legacy-windows-console"),
    ],
)
def test_log_display_preserves_output_on_console_encodings(
    encoding, expected, monkeypatch
) -> None:
    payload = "failure: ☃\n".encode()
    with (
        io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="\n") as console,
        monkeypatch.context() as environment,
    ):
        environment.setattr(sys, "stdout", console)
        test_process._display_output(io.BytesIO(payload))
        assert console.buffer.getvalue() == expected


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252"])
def test_runner_header_preserves_unicode_command_and_launches_pytest(
    encoding: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout-☃"
    root.mkdir()
    (root / "uv.lock").write_text("test lock", encoding="utf-8")
    interpreter = str(root / "python-☃")
    selection = "test_example.py::test_case[☃]"
    commands = []

    def successful_run(command, cwd, environment, log, *args):
        del cwd, environment, args
        commands.append(command)
        log.write_bytes("passed: ☃\n".encode())
        return test_process.ProcessOutcome(0, 0)

    monkeypatch.setattr(run_tests, "ROOT", root)
    monkeypatch.setattr(run_tests, "run_logged", successful_run)
    with (
        io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="\n") as console,
        monkeypatch.context() as environment,
    ):
        environment.setattr(sys, "stdout", console)
        environment.setattr(sys, "executable", interpreter)
        assert (
            run_tests.main(
                ["--profile", "nightly", "--hash-seed", "42", "--", selection]
            )
            == 0
        )
        (diagnostics,) = (root / "test-results").iterdir()
        metadata = json.loads((diagnostics / "metadata.json").read_text())
        header = f"Diagnostics: {diagnostics}\nReproduce: {metadata['reproduce']}\n"
        assert console.buffer.getvalue() == header.encode(
            encoding, errors="backslashreplace"
        )
    assert commands[0][0] == metadata["interpreter"] == interpreter
    assert commands[0][-1] == selection
    assert interpreter in metadata["reproduce"]
    assert selection in metadata["reproduce"]
    assert (diagnostics / "pytest.log").read_bytes() == "passed: ☃\n".encode()
