from __future__ import annotations

import subprocess
import sys

import pytest

from crewplane.observability.tmux.client import (
    TMUX_TIMEOUT_RETURN_CODE,
    TMUX_TIMEOUT_STDERR,
    TmuxCommandClient,
)


def test_unchecked_tmux_command_timeout_returns_sentinel() -> None:
    warnings: list[str] = []
    client = TmuxCommandClient(
        tmux_executable=sys.executable,
        timeout_seconds=0.01,
        warning_sink=warnings.append,
    )

    result = client.run(
        ["-c", "import time; time.sleep(10)"],
        capture_output=True,
        check=False,
    )

    assert result.returncode == TMUX_TIMEOUT_RETURN_CODE
    assert TMUX_TIMEOUT_STDERR in result.stderr
    assert warnings


def test_checked_tmux_command_timeout_raises() -> None:
    client = TmuxCommandClient(
        tmux_executable=sys.executable,
        timeout_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="tmux command timed out"):
        client.run(["-c", "import time; time.sleep(10)"], check=True)


def test_has_session_timeout_is_treated_as_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TmuxCommandClient(timeout_seconds=0.01)

    def timeout_run(
        *args: object,  # noqa: ARG001 - Required by subprocess.run monkeypatch.
        **kwargs: object,  # noqa: ARG001 - Required by subprocess.run monkeypatch.
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["tmux"], timeout=0.01)

    monkeypatch.setattr(subprocess, "run", timeout_run)

    assert client.session_exists("crewplane-run")


def test_pane_dimension_timeout_returns_default_and_marks_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TmuxCommandClient(timeout_seconds=0.01)

    def timeout_run(
        *args: object,  # noqa: ARG001 - Required by subprocess.run monkeypatch.
        **kwargs: object,  # noqa: ARG001 - Required by subprocess.run monkeypatch.
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["tmux"], timeout=0.01)

    monkeypatch.setattr(subprocess, "run", timeout_run)

    assert client.pane_dimension("%1", "#{pane_width}", 80) == (80, True)
