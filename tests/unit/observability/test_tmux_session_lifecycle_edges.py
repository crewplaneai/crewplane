import subprocess
from pathlib import Path

import pytest

from crewplane.observability.tmux.runtime_files import RuntimeFiles
from crewplane.observability.tmux.session import (
    TmuxSessionIdentity,
    TmuxSessionTargets,
)
from crewplane.observability.tmux.session_lifecycle import (
    RuntimeDirectoryLease,
    StartedCompactSession,
    TmuxCompactSessionLifecycle,
)


class StubTmux:
    def __init__(self, returncode: int = 0, fail: Exception | None = None) -> None:
        self.returncode = returncode
        self.fail = fail
        self.calls: list[list[str]] = []

    def run(
        self,
        args: list[str],
        capture_output: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, check
        self.calls.append(args)
        if self.fail is not None:
            raise self.fail
        return subprocess.CompletedProcess(
            ["tmux", *args],
            self.returncode,
            stdout="",
            stderr="failed" if self.returncode else "",
        )


class StubProcess:
    def __init__(self, waits: list[int | type[subprocess.TimeoutExpired]]) -> None:
        self.waits = waits
        self.terminate_count = 0
        self.kill_count = 0

    def wait(self, timeout: float) -> int:
        del timeout
        outcome = self.waits.pop(0)
        if outcome is subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired("attach", 1.0)
        return outcome

    def terminate(self) -> None:
        self.terminate_count += 1

    def kill(self) -> None:
        self.kill_count += 1


def process_factory(
    process: StubProcess,
    calls: list[tuple[tuple[object, ...], dict[str, object]]] | None = None,
):
    def spawn(*args: object, **kwargs: object) -> StubProcess:
        if calls is not None:
            calls.append((args, kwargs))
        return process

    return spawn


def started_session(tmp_path: Path, tmux: StubTmux) -> StartedCompactSession:
    root = tmp_path / "runtime"
    root.mkdir(parents=True)
    identity = TmuxSessionIdentity.from_run("run", socket_name="socket")
    return StartedCompactSession(
        runtime_lease=RuntimeDirectoryLease(root=root, preserve_on_stop=False),
        runtime_files=RuntimeFiles.from_root(root),
        targets=TmuxSessionTargets.from_identity(identity, "%1", "%2"),
        tmux=tmux,
    )


def test_runtime_directory_lease_preserves_or_forces_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    lease = RuntimeDirectoryLease(root=root, preserve_on_stop=True)

    lease.cleanup()
    assert root.exists()
    lease.cleanup(force=True)
    assert not root.exists()
    lease.cleanup(force=True)


def test_attach_switches_existing_tmux_client_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmux = StubTmux()
    session = started_session(tmp_path, tmux)
    lifecycle = TmuxCompactSessionLifecycle()
    monkeypatch.setenv("TMUX", "inside")

    def fail_spawn(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("attach fallback should not run")

    monkeypatch.setattr(
        "crewplane.observability.tmux.session_lifecycle.subprocess.Popen",
        fail_spawn,
    )

    lifecycle.attach_or_switch(session)

    assert session.attach_attempted
    assert tmux.calls == [["switch-client", "-t", session.targets.session_name]]


def test_attach_reports_last_candidate_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = started_session(tmp_path, StubTmux(returncode=1))
    lifecycle = TmuxCompactSessionLifecycle()
    monkeypatch.delenv("TMUX", raising=False)
    process = StubProcess([2])
    monkeypatch.setattr(
        "crewplane.observability.tmux.session_lifecycle.subprocess.Popen",
        process_factory(process),
    )

    with pytest.raises(RuntimeError, match="exited with code 2"):
        lifecycle.attach_or_switch(session)


def test_attach_candidate_timeout_retains_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = started_session(tmp_path, StubTmux(returncode=1))
    lifecycle = TmuxCompactSessionLifecycle()
    process = StubProcess([subprocess.TimeoutExpired])
    monkeypatch.setattr(
        "crewplane.observability.tmux.session_lifecycle.subprocess.Popen",
        process_factory(process),
    )

    lifecycle.attach_or_switch(session)

    assert session.attach_process is process


def test_attach_candidates_try_plain_fallback_and_scrub_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = started_session(tmp_path, StubTmux(returncode=1))
    lifecycle = TmuxCompactSessionLifecycle()
    monkeypatch.setenv("TMUX", "inside")
    process = StubProcess([1, subprocess.TimeoutExpired])
    spawn_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        "crewplane.observability.tmux.session_lifecycle.build_attach_command",
        lambda **kwargs: ["terminal-wrapper", kwargs["session_name"]],
    )
    monkeypatch.setattr(
        "crewplane.observability.tmux.session_lifecycle.subprocess.Popen",
        process_factory(process, spawn_calls),
    )

    lifecycle.attach_or_switch(session)

    assert len(spawn_calls) == 2
    assert spawn_calls[0][0][0][0] == "terminal-wrapper"
    assert spawn_calls[1][0][0][0] == "tmux"
    assert all("TMUX" not in call[1]["env"] for call in spawn_calls)


def test_attach_candidates_report_nonzero_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = started_session(tmp_path, StubTmux())
    lifecycle = TmuxCompactSessionLifecycle()
    process = StubProcess([3])
    monkeypatch.setattr(
        "crewplane.observability.tmux.session_lifecycle.subprocess.Popen",
        process_factory(process),
    )

    with pytest.raises(RuntimeError, match="exited with code 3"):
        lifecycle.attach_or_switch(session)
    assert session.attach_process is None


def test_rollback_terminates_attach_process_and_forces_cleanup(
    tmp_path: Path,
) -> None:
    session = started_session(tmp_path, StubTmux())
    lifecycle = TmuxCompactSessionLifecycle()
    process = StubProcess([subprocess.TimeoutExpired, subprocess.TimeoutExpired, 0])
    session.attach_process = process

    lifecycle.rollback_start(None)
    lifecycle.rollback_start(session)

    assert process.terminate_count == 1
    assert process.kill_count == 1
    assert session.attach_process is None
    assert not session.runtime_lease.root.exists()


def test_rollback_warns_when_tmux_kill_fails(
    tmp_path: Path,
) -> None:
    warnings: list[str] = []
    session = started_session(tmp_path, StubTmux(fail=OSError("unavailable")))
    lifecycle = TmuxCompactSessionLifecycle(warning_sink=warnings.append)

    lifecycle.rollback_start(session)

    assert warnings == ["tmux compact rollback failed: unavailable"]
    assert not session.runtime_lease.root.exists()


def test_stop_propagates_first_failure_after_cleanup(tmp_path: Path) -> None:
    failure = RuntimeError("kill failed")
    session = started_session(tmp_path, StubTmux(fail=failure))
    lifecycle = TmuxCompactSessionLifecycle()

    lifecycle.stop_session(None, auto_close_session=True)
    lifecycle.stop_session(session, auto_close_session=False)
    with pytest.raises(RuntimeError, match="kill failed"):
        lifecycle.stop_session(session, auto_close_session=True)

    assert not session.runtime_lease.root.exists()


def test_warning_sink_failure_is_suppressed_and_stderr_is_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_warning(_message: str) -> None:
        del _message
        raise RuntimeError("sink failed")

    session = started_session(
        tmp_path / "sink",
        StubTmux(fail=OSError("ignored")),
    )
    TmuxCompactSessionLifecycle(warning_sink=fail_warning).rollback_start(session)

    fallback_session = started_session(
        tmp_path / "fallback",
        StubTmux(fail=OSError("visible")),
    )
    TmuxCompactSessionLifecycle().rollback_start(fallback_session)

    assert capsys.readouterr().err == "WARN: tmux compact rollback failed: visible\n"
