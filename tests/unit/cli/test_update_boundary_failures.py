from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.cli.update import UpdateError, update_crewplane
from tests.unit.cli.update_helpers import FailedCommands, update_context


@pytest.mark.parametrize(
    ("phase", "error", "message"),
    [
        ("probe", OSError("missing"), "Could not run uv ownership probe"),
        ("probe", subprocess.TimeoutExpired("uv", 1), "ownership probe timed out"),
        ("update", OSError("missing"), "Failed to start"),
        ("verification", OSError("missing"), "verification could not start"),
        (
            "verification",
            subprocess.TimeoutExpired("python", 1),
            "verification timed out",
        ),
    ],
)
def test_update_reports_external_process_failures_without_retry(
    tmp_path: Path, phase: str, error: BaseException, message: str
) -> None:
    commands = FailedCommands({})
    context = update_context(tmp_path, commands)
    probe = ("uv", "tool", "dir")
    update = ("uv", "tool", "upgrade", "crewplane")
    verification = (
        str(context.python_executable),
        "-c",
        "from importlib.metadata import version; import sys; print(version(sys.argv[1]))",
        "crewplane",
    )
    order = [probe, update, verification]
    phases = {"probe": 0, "update": 1, "verification": 2}
    commands.responses = {
        probe: str(context.environment_root.parent),
        update: "",
        verification: "1.1",
    }
    commands.responses[order[phases[phase]]] = error

    with pytest.raises(UpdateError, match=message) as caught:
        update_crewplane(Console(file=StringIO()), context)

    assert caught.value.__cause__ is error
    assert commands.calls == order[: phases[phase] + 1]


@pytest.mark.parametrize("phase", ["probe", "verification"])
def test_update_interruption_reports_whether_manager_completed(
    tmp_path: Path, phase: str
) -> None:
    commands = FailedCommands({})
    context = update_context(tmp_path, commands)
    probe = ("uv", "tool", "dir")
    update = ("uv", "tool", "upgrade", "crewplane")
    verification = (
        str(context.python_executable),
        "-c",
        "from importlib.metadata import version; import sys; print(version(sys.argv[1]))",
        "crewplane",
    )
    commands.responses = {
        probe: str(context.environment_root.parent),
        update: "",
        verification: "1.1",
    }
    commands.responses[probe if phase == "probe" else verification] = (
        KeyboardInterrupt()
    )
    output = StringIO()

    assert update_crewplane(Console(file=output, width=160), context) == 130

    expected = (
        "before the package manager ran"
        if phase == "probe"
        else "package manager completed"
    )
    assert expected in output.getvalue()
    assert len(commands.calls) == (1 if phase == "probe" else 3)


@pytest.mark.parametrize(
    "output", ["", "1.1\nextra\n", "different-package 1.1", "one two three"]
)
def test_update_rejects_untrustworthy_version_probe_output(
    tmp_path: Path, output: str
) -> None:
    commands = FailedCommands({})
    context = update_context(tmp_path, commands)
    verification = (
        str(context.python_executable),
        "-c",
        "from importlib.metadata import version; import sys; print(version(sys.argv[1]))",
        "crewplane",
    )
    commands.responses = {
        ("uv", "tool", "dir"): str(context.environment_root.parent),
        ("uv", "tool", "upgrade", "crewplane"): "",
        verification: output,
    }
    terminal = StringIO()

    with pytest.raises(UpdateError, match="version verification returned"):
        update_crewplane(Console(file=terminal), context)

    assert "updated from" not in terminal.getvalue()
    assert len(commands.calls) == 3
