from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import Distribution, PathDistribution
from io import StringIO
from pathlib import Path
from typing import cast

import pytest
from rich.console import Console

from crewplane.cli.update import default_update_context, update_crewplane
from crewplane.cli.update import runner as update_runner
from crewplane.cli.update.types import CommandRunner, UpdateCommand


@dataclass
class ManagerCommands:
    outputs: dict[UpdateCommand, str]
    calls: list[tuple[UpdateCommand, dict[str, object]]] = field(default_factory=list)

    def __call__(
        self, args: Sequence[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        arguments = tuple(args)
        self.calls.append((arguments, options))
        return subprocess.CompletedProcess(
            list(arguments), 0, stdout=self.outputs[arguments], stderr=""
        )


@dataclass(frozen=True)
class WheelInstallation:
    environment_root: Path
    manager: str
    installer: str
    owner_label: str
    probe_command: UpdateCommand
    probe_output: str
    update_command: UpdateCommand
    verification_command: UpdateCommand


def wheel_installation(root: Path, owner: str) -> WheelInstallation:
    if owner == "uv":
        environment_root = root / "uv" / "tools" / "crewplane"
        environment_root.mkdir(parents=True)
        (environment_root / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "crewplane" }]\n', encoding="utf-8"
        )
        return WheelInstallation(
            environment_root,
            "uv",
            "uv",
            "uv tool",
            ("uv", "tool", "dir"),
            str(environment_root.parent),
            ("uv", "tool", "upgrade", "crewplane"),
            python_verification_command(environment_root),
        )
    if owner == "pipx":
        environment_root = root / "pipx" / "venvs" / "crewplane"
        environment_root.mkdir(parents=True)
        (environment_root / "pipx_metadata.json").write_text(
            json.dumps(
                {
                    "main_package": {
                        "package": "crewplane",
                        "package_or_url": "crewplane",
                    }
                }
            ),
            encoding="utf-8",
        )
        return WheelInstallation(
            environment_root,
            "pipx",
            "pip",
            "pipx",
            ("pipx", "environment", "--value", "PIPX_HOME"),
            str(root / "pipx"),
            ("pipx", "upgrade", "crewplane"),
            python_verification_command(environment_root),
        )
    assert owner == "homebrew"
    formula_prefix = root / "Cellar" / "crewplane" / "0.1.1"
    environment_root = formula_prefix / "libexec"
    environment_root.mkdir(parents=True)
    return WheelInstallation(
        environment_root,
        "brew",
        "pip",
        "Homebrew",
        ("brew", "--prefix", "crewplane"),
        str(formula_prefix),
        ("brew", "upgrade", "crewplane"),
        (str(formula_prefix / "bin" / "crewplane"), "--version"),
    )


def python_verification_command(environment_root: Path) -> UpdateCommand:
    return (
        str(environment_root / "bin" / "python"),
        "-c",
        "from importlib.metadata import version; import sys; print(version(sys.argv[1]))",
        "crewplane",
    )


@pytest.mark.parametrize("owner", ["uv", "pipx", "homebrew"])
def test_wheel_metadata_resolves_owner_and_completes_verified_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    installation = wheel_installation(tmp_path, owner)
    metadata_dir = installation.environment_root / "crewplane-0.1.1.dist-info"
    metadata_dir.mkdir()
    (metadata_dir / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: crewplane\nVersion: 0.1.1\n", encoding="utf-8"
    )
    (metadata_dir / "INSTALLER").write_text(
        installation.installer + "\n", encoding="utf-8"
    )

    def installed_distribution(name: str) -> Distribution:
        assert name == "crewplane"
        return PathDistribution(metadata_dir)

    monkeypatch.setattr(update_runner, "distribution", installed_distribution)
    context = default_update_context()
    assert context.current_version == "0.1.1"
    assert context.metadata.installer == installation.installer
    assert context.metadata.editable is False
    assert context.metadata.direct_source is False
    assert not (metadata_dir / "direct_url.json").exists()

    verification_output = "crewplane 0.1.2\n" if owner == "homebrew" else "0.1.2\n"
    commands = ManagerCommands(
        {
            installation.probe_command: installation.probe_output + "\n",
            installation.update_command: "",
            installation.verification_command: verification_output,
        }
    )
    executables = {installation.manager: str(tmp_path / "bin" / installation.manager)}
    context = replace(
        context,
        environment_root=installation.environment_root,
        python_executable=installation.environment_root / "bin" / "python",
        executable_lookup=executables.get,
        command_runner=cast(CommandRunner, commands),
    )
    output = StringIO()

    status = update_crewplane(Console(file=output, width=160), context)

    assert status == 0
    assert [command for command, options in commands.calls] == [
        installation.probe_command,
        installation.update_command,
        installation.verification_command,
    ]
    assert all(options["shell"] is False for command, options in commands.calls)
    assert commands.calls[1][1] == {"check": False, "shell": False}
    assert commands.calls[2][1]["capture_output"] is True
    assert commands.calls[2][1]["text"] is True
    assert isinstance(commands.calls[2][1]["timeout"], (int, float))
    assert (
        f"updated from version 0.1.1 to 0.1.2 through {installation.owner_label}"
        in (output.getvalue())
    )
