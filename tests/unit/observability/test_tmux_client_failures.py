from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.observability.tmux.client import TmuxCommandClient
from crewplane.observability.tmux.runtime_files import read_index, read_runtime_mode


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [(1, "40", 12), (0, "not-a-number", 12), (0, "0", 1), (0, " 42\n", 42)],
)
def test_pane_dimension_uses_default_for_invalid_response(
    returncode: int, output: str, expected: int
) -> None:
    result = subprocess.CompletedProcess([], returncode, stdout=output)
    client = TmuxCommandClient(socket_name="test-socket")
    with patch("subprocess.run", return_value=result) as run:
        assert client.pane_dimension("%2", "#{pane_width}", 12) == (expected, False)

    assert client.socket_name == "test-socket"
    assert run.call_args.args[0] == [
        "tmux",
        "-L",
        "test-socket",
        "display-message",
        "-p",
        "-t",
        "%2",
        "#{pane_width}",
    ]


def test_repeated_tmux_timeouts_warn_once_and_preserve_session() -> None:
    warnings: list[str] = []
    client = TmuxCommandClient(warning_sink=warnings.append)
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tmux", 1)):
        assert client.pane_dimension("%2", "#{pane_width}", 12) == (12, True)
        assert client.session_exists("workflow")
        with pytest.raises(RuntimeError, match="tmux command timed out"):
            client.run(["list-panes"])

    assert len(warnings) == 1
    assert "live dashboard may be stale" in warnings[0]


@pytest.mark.parametrize(
    ("content", "expected"),
    [(None, -1), ("", -1), ("broken", -1), ("-1", -1), ("3", 3)],
)
def test_runtime_index_tolerates_missing_or_invalid_file(
    tmp_path: Path, content: str | None, expected: int
) -> None:
    path = tmp_path / "index.txt"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    assert read_index(path) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, "dashboard"),
        ("", "dashboard"),
        ("unknown", "dashboard"),
        ("dashboard", "dashboard"),
        ("inspect", "inspect"),
    ],
)
def test_runtime_mode_defaults_to_dashboard(
    tmp_path: Path, content: str | None, expected: str
) -> None:
    path = tmp_path / "mode.txt"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    assert read_runtime_mode(path) == expected
