from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.cli.update import (
    UpdateError,
    default_update_context,
    update_crewplane,
)
from crewplane.cli.update import (
    runner as update_module,
)
from crewplane.cli.update.types import (
    InstalledMetadata,
    UpdateCommand,
    UpdateContext,
    UpdatePlan,
)

PACKAGE_NAME = "crewplane"
METADATA_PACKAGE_NAME = "crewplane-from-project-metadata"
CURRENT_VERSION = "0.1.1"
UPDATED_VERSION = "0.1.2"
UPDATE_COMMAND: UpdateCommand = ("uv", "tool", "upgrade", PACKAGE_NAME)
VERSION_PROBE_SCRIPT = "print('version')"


class FakeDistribution:
    version = CURRENT_VERSION
    metadata = {"Name": METADATA_PACKAGE_NAME}

    def read_text(self, filename: str) -> str | None:
        return {
            "INSTALLER": "uv\n",
            "direct_url.json": (
                '{"url":"file:///project/crewplane","dir_info":{"editable":true}}'
            ),
        }.get(filename)


@dataclass
class FakeRunner:
    responses: dict[
        UpdateCommand,
        subprocess.CompletedProcess[str] | BaseException,
    ]
    calls: list[tuple[UpdateCommand, dict[str, object]]] = field(default_factory=list)

    def __call__(
        self,
        command: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        argv = tuple(command)
        self.calls.append((argv, dict(kwargs)))
        response = self.responses[argv]
        if isinstance(response, BaseException):
            raise response
        return response


def completed(
    command: UpdateCommand,
    returncode: int = 0,
    stdout: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        list(command),
        returncode,
        stdout=stdout,
        stderr="",
    )


def make_context(
    tmp_path: Path,
    runner: FakeRunner,
    current_version: str = CURRENT_VERSION,
) -> UpdateContext:
    environment_root = tmp_path / "tools" / "crewplane"
    return UpdateContext(
        package_name=PACKAGE_NAME,
        python_executable=environment_root / "bin" / "python",
        environment_root=environment_root,
        metadata=InstalledMetadata(
            installer="uv",
            editable=False,
            direct_source=False,
        ),
        current_version=current_version,
        executable_lookup=lambda executable: f"/usr/bin/{executable}",
        command_runner=runner,
    )


def make_plan(context: UpdateContext) -> UpdatePlan:
    verification_command = (
        str(context.python_executable),
        "-c",
        VERSION_PROBE_SCRIPT,
        context.package_name,
    )
    return UpdatePlan(
        owner="uv tool",
        command=UPDATE_COMMAND,
        verification_command=verification_command,
    )


def captured_console(buffer: StringIO) -> Console:
    return Console(file=buffer, force_terminal=False, width=120)


def install_plan(
    monkeypatch: pytest.MonkeyPatch,
    plan: UpdatePlan,
) -> None:
    def fake_resolve(context: UpdateContext) -> UpdatePlan:
        assert isinstance(context, UpdateContext)
        return plan

    monkeypatch.setattr(
        update_module,
        "resolve_update_plan",
        fake_resolve,
    )


def test_default_context_reads_installed_version_and_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = FakeDistribution()

    def installed_distribution(name: str) -> FakeDistribution:
        assert name == update_module.__name__.partition(".")[0]
        return package

    monkeypatch.setattr(update_module, "distribution", installed_distribution)

    context = default_update_context()

    assert context.package_name == METADATA_PACKAGE_NAME
    assert context.current_version == CURRENT_VERSION
    assert context.metadata.installer == "uv"
    assert context.metadata.editable
    assert context.metadata.direct_source


def test_update_delegates_then_verifies_in_a_fresh_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeRunner({})
    context = make_context(tmp_path, runner)
    plan = make_plan(context)
    runner.responses.update(
        {
            plan.command: completed(plan.command),
            plan.verification_command: completed(
                plan.verification_command,
                stdout=f"{UPDATED_VERSION}\n",
            ),
        }
    )
    install_plan(monkeypatch, plan)
    output = StringIO()

    assert update_crewplane(captured_console(output), context) == 0

    assert [command for command, _kwargs in runner.calls] == [
        plan.command,
        plan.verification_command,
    ]
    assert all(kwargs["shell"] is False for _command, kwargs in runner.calls)
    assert runner.calls[0][1].get("capture_output") is not True
    assert runner.calls[1][1]["capture_output"] is True
    assert runner.calls[1][1]["text"] is True
    assert runner.calls[1][1]["timeout"] > 0
    rendered_output = output.getvalue()
    assert CURRENT_VERSION in rendered_output
    assert UPDATED_VERSION in rendered_output
    assert "updated" in rendered_output.casefold()


def test_same_version_after_manager_success_is_successful(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeRunner({})
    context = make_context(tmp_path, runner)
    plan = make_plan(context)
    runner.responses.update(
        {
            plan.command: completed(plan.command),
            plan.verification_command: completed(
                plan.verification_command,
                stdout=f"{CURRENT_VERSION}\n",
            ),
        }
    )
    install_plan(monkeypatch, plan)
    output = StringIO()

    assert update_crewplane(captured_console(output), context) == 0

    rendered_output = output.getvalue().casefold()
    assert "remains at version" in rendered_output
    assert "may already be current" in rendered_output


def test_homebrew_verification_accepts_cli_version_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    update_command: UpdateCommand = ("brew", "upgrade", "crewplane")
    verification_command: UpdateCommand = ("crewplane", "--version")
    plan = UpdatePlan(
        owner="Homebrew",
        command=update_command,
        verification_command=verification_command,
    )
    runner = FakeRunner(
        {
            update_command: completed(update_command),
            verification_command: completed(
                verification_command,
                stdout=f"crewplane {UPDATED_VERSION}\n",
            ),
        }
    )
    context = make_context(tmp_path, runner)
    install_plan(monkeypatch, plan)
    output = StringIO()

    assert update_crewplane(captured_console(output), context) == 0

    assert UPDATED_VERSION in output.getvalue()


@pytest.mark.parametrize(
    ("manager_status", "expected_status"),
    [(23, 23), (-9, 137)],
)
def test_manager_failure_preserves_status_and_skips_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manager_status: int,
    expected_status: int,
) -> None:
    runner = FakeRunner({UPDATE_COMMAND: completed(UPDATE_COMMAND, manager_status)})
    context = make_context(tmp_path, runner)
    plan = make_plan(context)
    install_plan(monkeypatch, plan)
    output = StringIO()

    assert update_crewplane(captured_console(output), context) == expected_status

    assert [command for command, _kwargs in runner.calls] == [plan.command]
    rendered_output = output.getvalue()
    assert "did not retry, elevate, or alter" in rendered_output
    assert "package-manager and administrator policy" in rendered_output
    assert "uv tool upgrade crewplane" in rendered_output
    assert "Retry manually" not in rendered_output


def test_failed_fresh_process_verification_does_not_claim_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeRunner({})
    context = make_context(tmp_path, runner)
    plan = make_plan(context)
    runner.responses.update(
        {
            plan.command: completed(plan.command),
            plan.verification_command: completed(
                plan.verification_command,
                returncode=1,
            ),
        }
    )
    install_plan(monkeypatch, plan)
    output = StringIO()

    with pytest.raises(UpdateError):
        update_crewplane(captured_console(output), context)

    assert "completed" not in output.getvalue().casefold()


def test_interrupted_update_returns_sigint_status_without_verifying(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeRunner({UPDATE_COMMAND: KeyboardInterrupt()})
    context = make_context(tmp_path, runner)
    plan = make_plan(context)
    install_plan(monkeypatch, plan)
    output = StringIO()

    assert update_crewplane(captured_console(output), context) == 130

    assert [command for command, _kwargs in runner.calls] == [plan.command]
    assert "interrupted" in output.getvalue().casefold()
