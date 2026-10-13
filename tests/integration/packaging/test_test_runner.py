from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from textwrap import dedent
from unittest.mock import Mock

import pytest

from tests.helpers.fixture_files import copy_fixture
from tests.helpers.installed_package_assertions import process_running
from tests.helpers.platforms import requires_posix
from tests.helpers.test_runner_project import (
    kill_active_process_groups,
    run_fixture,
    runner_environment,
    runner_project,
)


def process_stopped(pid: int) -> bool:
    # A reparented POSIX zombie is stopped; the host init owns its final wait.
    with suppress(FileNotFoundError, ProcessLookupError):
        if Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
            return True
    return not process_running(pid)


@pytest.mark.parametrize("read_error", [FileNotFoundError, ProcessLookupError])
@pytest.mark.parametrize("running", [False, True])
def test_process_stopped_rechecks_missing_stat(
    monkeypatch: pytest.MonkeyPatch, read_error: type[OSError], running: bool
) -> None:
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=read_error))
    monkeypatch.setattr(
        sys.modules[__name__], "process_running", Mock(return_value=running)
    )
    assert process_stopped(123) is not running


@pytest.mark.parametrize(
    ("source", "workers", "expected"),
    [
        pytest.param(
            "def test_ok() -> None:\n    assert True\n",
            0,
            0,
            id="serial-success",
        ),
        pytest.param(
            "def test_ok() -> None:\n    assert True\n",
            2,
            0,
            id="parallel-success",
        ),
        pytest.param(
            'def test_bad() -> None:\n    raise AssertionError("fixture failure")\n',
            0,
            1,
            id="assertion-failure",
        ),
        pytest.param(
            'raise RuntimeError("collection failed")\n',
            0,
            2,
            id="collection-failure",
        ),
        pytest.param(
            "import os\n\ndef test_crash() -> None:\n    os._exit(7)\n",
            2,
            1,
            id="worker-crash",
        ),
    ],
)
def test_runner_propagates_outcomes_and_retains_diagnostics(
    tmp_path: Path, source: str, workers: int, expected: int
) -> None:
    script = runner_project(tmp_path)
    (tmp_path / "test_example.py").write_text(source, encoding="utf-8")
    result = run_fixture(script, "--workers", str(workers))
    assert result.returncode == expected, result.stdout + result.stderr
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    metadata = json.loads((diagnostics / "metadata.json").read_text())
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert metadata["hash_seed"] == 42
    assert metadata["workers"] == workers
    assert metadata["interpreter"] == sys.executable
    assert metadata["dependencies"]["pytest"]
    assert len(metadata["lock_sha256"]) == 64
    assert outcome["exit_code"] == expected
    assert (diagnostics / "pytest.log").stat().st_size > 0
    assert (diagnostics / "junit.xml").is_file()
    assert (diagnostics / "durations.txt").is_file()


@pytest.mark.parametrize(
    "selected_path", [False, True], ids=["discovery", "path-filter"]
)
@pytest.mark.parametrize(("profile", "expected"), [("race", 0), ("nightly", 1)])
def test_runner_selects_marked_tests_after_module_moves(
    tmp_path: Path, selected_path: bool, profile: str, expected: int
) -> None:
    script = runner_project(tmp_path)
    (tmp_path / "test_example.py").write_text(
        dedent("""\
            import pytest

            @pytest.mark.race
            def test_interleaving() -> None:
                pass

            def test_unmarked() -> None:
                raise AssertionError("unmarked test ran")
            """),
        encoding="utf-8",
    )
    moved = tmp_path / "new_component" / "test_renamed.py"
    moved.parent.mkdir()
    (tmp_path / "test_example.py").rename(moved)
    selection = [str(moved)] if selected_path else []

    result = run_fixture(script, "--profile", profile, *selection)

    assert result.returncode == expected, result.stdout + result.stderr
    expected_summary = (
        "1 passed, 1 deselected" if profile == "race" else "1 failed, 1 passed"
    )
    assert expected_summary in result.stdout
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    metadata = json.loads((diagnostics / "metadata.json").read_text())
    assert metadata["marker_expression"] == ("race" if profile == "race" else "")
    assert metadata["selection"] == selection


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "def test_ok() -> None:\n    assert True\n",
            5,
            id="empty-selection",
        ),
        pytest.param(
            "import pytest\n\n"
            "@pytest.mark.race\n"
            "def test_race() -> None:\n"
            '    raise AssertionError("marked fixture failure")\n',
            1,
            id="test-failure",
        ),
        pytest.param(
            "import pytest\n\n@pytest.mark.raec\ndef test_typo() -> None:\n    pass\n",
            2,
            id="unknown-marker",
        ),
    ],
)
def test_race_profile_propagates_selection_and_test_failures(
    tmp_path: Path, source: str, expected: int
) -> None:
    script = runner_project(tmp_path)
    (tmp_path / "test_example.py").write_text(source, encoding="utf-8")

    result = run_fixture(script, "--profile", "race")

    assert result.returncode == expected, result.stdout + result.stderr
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert outcome["pytest"]["returncode"] == expected
    assert outcome["exit_code"] == expected


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_runner_timeout_terminates_ignoring_child_and_reaps_parent(
    tmp_path, workers
) -> None:
    script = runner_project(tmp_path)
    copy_fixture("test_runner/blocking_tree.py", tmp_path / "test_example.py")
    (tmp_path / "conftest.py").write_text(
        dedent("""\
            import os
            from pathlib import Path

            def pytest_sessionstart() -> None:
                Path(f"session-{os.getpid()}.pid").write_text(
                    str(os.getpid()), encoding="utf-8"
                )
            """),
        encoding="utf-8",
    )
    if os.name == "nt":
        # Withhold cooperative console shutdown to exercise real Job escalation.
        script = copy_fixture(
            "test_runner/force_shutdown_runner.py",
            script.with_name("force_shutdown_runner.py"),
        )
    result = run_fixture(
        script,
        "--workers",
        str(workers),
        "--timeout",
        "5",
        "--shutdown-timeout",
        "0.3",
    )
    assert result.returncode != 0
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert outcome["pytest"]["timed_out"] is True
    assert outcome["pytest"]["forced"] is True
    sessions = list(tmp_path.glob("session-*.pid"))
    assert len(sessions) == (3 if workers else 1)
    for receipt in [*sessions, tmp_path / "child.pid"]:
        pid = int(receipt.read_text())
        assert process_stopped(pid)


@requires_posix
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_runner_timeout_terminates_isolated_provider(
    tmp_path: Path, workers: int
) -> None:
    script = runner_project(tmp_path)
    copy_fixture("test_runner/isolated_provider.py", tmp_path / "test_example.py")
    receipt = tmp_path / "provider.pid"
    try:
        result = run_fixture(
            script,
            "--workers",
            str(workers),
            "--timeout",
            "5",
            "--shutdown-timeout",
            "0.3",
        )
        assert result.returncode != 0, result.stdout + result.stderr
        (diagnostics,) = (tmp_path / "test-results").iterdir()
        outcome = json.loads((diagnostics / "outcome.json").read_text())
        assert outcome["pytest"]["timed_out"] is True
        assert outcome["pytest"]["forced"] is True
        pid = int(receipt.read_text())
        deadline = time.monotonic() + 5
        while not process_stopped(pid):
            assert time.monotonic() < deadline, "isolated provider survived shutdown"
            time.sleep(0.05)
        assert not list(diagnostics.glob("*.active-pid"))
    finally:
        if receipt.exists():
            with suppress(ProcessLookupError):
                os.killpg(int(receipt.read_text()), signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux child adoption")
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_runner_reaps_isolated_providers_after_worker_crash(
    tmp_path: Path, workers: int
) -> None:
    script = runner_project(tmp_path)
    copy_fixture("test_runner/orphaned_provider.py", tmp_path / "test_example.py")
    receipts = [tmp_path / name for name in ("provider.pid", "child.pid")]
    try:
        result = run_fixture(script, "--workers", str(workers))
        assert result.returncode == (7 if workers == 0 else 1), (
            result.stdout + result.stderr
        )
        for receipt in receipts:
            assert not process_running(int(receipt.read_text())), (
                f"{receipt.name} survived the worker crash"
            )
        (diagnostics,) = (tmp_path / "test-results").iterdir()
        outcome = json.loads((diagnostics / "outcome.json").read_text())
        assert outcome["pytest"]["forced"] is True
        assert outcome["pytest"]["timed_out"] is False
        assert not list(diagnostics.glob("*.active-pid"))
    finally:
        for receipt in receipts:
            if receipt.exists():
                with suppress(ProcessLookupError):
                    os.killpg(int(receipt.read_text()), signal.SIGKILL)


@requires_posix
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
@pytest.mark.parametrize(
    "responsive", [True, False], ids=["graceful-shutdown", "forced-shutdown"]
)
def test_outer_fixture_timeout_terminates_launcher_pytest_and_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workers: int,
    responsive: bool,
) -> None:
    script = runner_project(tmp_path)
    copy_fixture("test_runner/blocking_tree.py", tmp_path / "test_example.py")
    (tmp_path / "conftest.py").write_text(
        dedent("""\
            import os
            from pathlib import Path

            def pytest_sessionstart() -> None:
                Path(f"session-{os.getpid()}.pid").write_text(
                    str(os.getpid()), encoding="utf-8"
                )
            """),
        encoding="utf-8",
    )
    if not responsive:
        script = copy_fixture(
            "test_runner/unresponsive_runner.py",
            script.with_name("unresponsive_runner.py"),
        )

    launchers = []

    class TimeoutWhenReady(subprocess.Popen):
        timeout_triggered = False

        def communicate(self, input=None, timeout=None):
            if not self.timeout_triggered:
                launchers.append(self)
                deadline = time.monotonic() + 15
                while not (tmp_path / "child.pid").exists():
                    assert self.poll() is None
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                self.timeout_triggered = True
                timeout = 0.1
            elif not responsive:
                timeout = 0.1
            return super().communicate(input=input, timeout=timeout)

    monkeypatch.setattr(subprocess, "Popen", TimeoutWhenReady)
    try:
        with pytest.raises(subprocess.TimeoutExpired) as captured:
            run_fixture(script, "--workers", str(workers), "--shutdown-timeout", "0.3")
        (launcher,) = launchers
        assert launcher.returncode is not None
        assert captured.value.stdout
        sessions = list(tmp_path.glob("session-*.pid"))
        assert len(sessions) == (3 if workers else 1)
        for pid_file in [*sessions, tmp_path / "child.pid"]:
            pid = int(pid_file.read_text())
            deadline = time.monotonic() + 5
            while not process_stopped(pid):
                assert time.monotonic() < deadline
                time.sleep(0.05)
        (diagnostics,) = (tmp_path / "test-results").iterdir()
        assert (diagnostics / "metadata.json").is_file()
        assert (diagnostics / "pytest.log").is_file()
        assert not list(diagnostics.glob("*.active-pid"))
        if responsive:
            outcome = json.loads((diagnostics / "outcome.json").read_text())
            assert outcome["pytest"]["interrupted"] is True
            assert outcome["pytest"]["forced"] is True
    finally:
        for pid_file in tmp_path.glob("session-*.pid"):
            with suppress(ProcessLookupError):
                group_id = os.getpgid(int(pid_file.read_text()))
                assert group_id != os.getpgrp()
                os.killpg(group_id, signal.SIGKILL)


@pytest.fixture
def interruptible_runner(tmp_path: Path) -> Path:
    script = runner_project(tmp_path)
    (tmp_path / "test_example.py").write_text(
        dedent("""\
            import time
            from pathlib import Path

            def test_block() -> None:
                Path("ready").touch()
                time.sleep(60)
            """),
        encoding="utf-8",
    )
    return script


def interrupt_runner(script: Path) -> int:
    root = script.parent.parent
    command = [
        sys.executable,
        str(script),
        "--profile",
        "nightly",
        "--shutdown-timeout",
        "1",
    ]
    with (root / "launcher.log").open("w") as output:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=runner_environment(),
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 15
            while not (root / "ready").exists():
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.05)
            process.send_signal(
                signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
            )
            return process.wait(timeout=15)
        finally:
            kill_active_process_groups(root)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_runner_interrupt_preserves_diagnostics(
    tmp_path: Path, interruptible_runner: Path
) -> None:
    assert interrupt_runner(interruptible_runner) != 0

    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert outcome["pytest"]["interrupted"] is True
    assert (diagnostics / "pytest.log").is_file()


@requires_posix
def test_interruption_fixture_timeout_cleans_pytest_group(
    tmp_path: Path, interruptible_runner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = copy_fixture(
        "test_runner/unresponsive_runner.py",
        interruptible_runner.with_name("unresponsive_runner.py"),
    )
    (tmp_path / "conftest.py").write_text(
        dedent("""\
            import os
            from pathlib import Path

            def pytest_sessionstart() -> None:
                Path("session.pid").write_text(str(os.getpid()), encoding="utf-8")
            """),
        encoding="utf-8",
    )
    launchers = []

    class TimeoutOnInterrupt(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            launchers.append(self)

        def wait(self, timeout=None):
            return super().wait(timeout=0.1 if timeout == 15 else timeout)

    monkeypatch.setattr(subprocess, "Popen", TimeoutOnInterrupt)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            interrupt_runner(script)
        (launcher,) = launchers
        assert launcher.returncode is not None
        pid = int((tmp_path / "session.pid").read_text())
        deadline = time.monotonic() + 5
        while not process_stopped(pid):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        (diagnostics,) = (tmp_path / "test-results").iterdir()
        assert not list(diagnostics.glob("*.active-pid"))
        assert (diagnostics / "metadata.json").is_file()
        assert (diagnostics / "pytest.log").is_file()
    finally:
        kill_active_process_groups(tmp_path)
