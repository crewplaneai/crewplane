from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from crewplane.cli.update import UpdateError
from crewplane.cli.update.detection import resolve_update_plan
from crewplane.cli.update.types import (
    InstalledMetadata,
    UpdateCommand,
    UpdateContext,
)

PACKAGE_NAME = "crewplane"
VERSION_PROBE_SCRIPT = (
    "from importlib.metadata import version; import sys; print(version(sys.argv[1]))"
)


@dataclass
class ProbeRunner:
    outputs: dict[UpdateCommand, str]
    calls: list[tuple[UpdateCommand, dict[str, object]]] = field(default_factory=list)

    def __call__(
        self,
        command: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        argv = tuple(command)
        self.calls.append((argv, dict(kwargs)))
        stdout = self.outputs.get(argv)
        return subprocess.CompletedProcess(
            list(argv),
            0 if stdout is not None else 1,
            stdout=stdout or "",
            stderr="",
        )


def make_context(
    environment_root: Path,
    runner: ProbeRunner,
    installer: str | None,
    executables: set[str],
    editable: bool = False,
    direct_source: bool = False,
) -> UpdateContext:
    return UpdateContext(
        package_name=PACKAGE_NAME,
        python_executable=environment_root / "bin" / "python",
        environment_root=environment_root,
        metadata=InstalledMetadata(
            installer=installer,
            editable=editable,
            direct_source=direct_source,
        ),
        current_version="0.1.1",
        executable_lookup=lambda executable: (
            f"/usr/bin/{executable}" if executable in executables else None
        ),
        command_runner=runner,
    )


def expected_python_verification(context: UpdateContext) -> UpdateCommand:
    return (
        str(context.python_executable),
        "-c",
        VERSION_PROBE_SCRIPT,
        context.package_name,
    )


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_uv_receipt(environment_root: Path) -> None:
    environment_root.mkdir(parents=True, exist_ok=True)
    (environment_root / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "crewplane" }]\n',
        encoding="utf-8",
    )


def write_pipx_metadata(
    environment_root: Path,
    suffix: str | None = None,
) -> None:
    main_package: dict[str, object] = {
        "package": "crewplane",
        "package_or_url": "crewplane",
    }
    if suffix is not None:
        main_package["suffix"] = suffix
    write_json(
        environment_root / "pipx_metadata.json",
        {"main_package": main_package},
    )


def write_npm_package(package_root: Path) -> None:
    write_json(
        package_root / "package.json",
        {
            "name": "crewplane",
            "crewplane": {"pythonConsoleCommand": "crewplane"},
        },
    )


def test_uv_tool_install_delegates_to_uv(
    tmp_path: Path,
) -> None:
    tool_root = tmp_path / "uv" / "tools"
    environment_root = tool_root / "crewplane"
    write_uv_receipt(environment_root)
    runner = ProbeRunner({("uv", "tool", "dir"): f"{tool_root}\n"})
    context = make_context(environment_root, runner, "uv", {"uv"})

    plan = resolve_update_plan(context)

    assert plan.owner == "uv tool"
    assert plan.command == ("uv", "tool", "upgrade", "crewplane")
    assert plan.verification_command == expected_python_verification(context)
    assert runner.calls[0][1]["shell"] is False


@pytest.mark.parametrize(
    ("environment_name", "global_install"),
    [
        ("crewplane", False),
        ("crewplane-review", False),
        ("crewplane", True),
    ],
)
def test_pipx_install_delegates_to_its_recorded_scope(
    tmp_path: Path,
    environment_name: str,
    global_install: bool,
) -> None:
    local_home = tmp_path / "pipx"
    owner_home = tmp_path / ("global-pipx" if global_install else "pipx")
    environment_root = owner_home / "venvs" / environment_name
    suffix = environment_name.removeprefix("crewplane") or None
    write_pipx_metadata(environment_root, suffix)
    outputs = {("pipx", "environment", "--value", "PIPX_HOME"): f"{local_home}\n"}
    if global_install:
        outputs[("pipx", "environment", "--value", "PIPX_GLOBAL_HOME")] = (
            f"{owner_home}\n"
        )
    runner = ProbeRunner(outputs)
    context = make_context(environment_root, runner, "pip", {"pipx"})

    plan = resolve_update_plan(context)

    expected: UpdateCommand = ("pipx", "upgrade", environment_name)
    if global_install:
        expected = ("pipx", "upgrade", "--global", environment_name)
    assert plan.owner == "pipx"
    assert plan.command == expected
    assert plan.verification_command == expected_python_verification(context)


def test_homebrew_install_delegates_to_formula_owner(
    tmp_path: Path,
) -> None:
    cellar_prefix = tmp_path / "Cellar" / "crewplane" / "0.1.1"
    environment_root = cellar_prefix / "libexec"
    environment_root.mkdir(parents=True)
    formula_prefix = tmp_path / "opt" / "crewplane"
    formula_prefix.parent.mkdir()
    formula_prefix.symlink_to(cellar_prefix, target_is_directory=True)
    runner = ProbeRunner({("brew", "--prefix", "crewplane"): f"{formula_prefix}\n"})
    context = make_context(environment_root, runner, None, {"brew"})

    plan = resolve_update_plan(context)

    assert plan.owner == "Homebrew"
    assert plan.command == ("brew", "upgrade", "crewplane")
    assert plan.verification_command == (
        str(formula_prefix / "bin" / "crewplane"),
        "--version",
    )


def test_global_npm_wrapper_requires_manual_lifecycle_aware_update(
    tmp_path: Path,
) -> None:
    npm_root = tmp_path / "lib" / "node_modules"
    package_root = npm_root / "crewplane"
    environment_root = package_root / ".venv"
    write_npm_package(package_root)
    runner = ProbeRunner({("npm", "root", "--global"): f"{npm_root}\n"})
    context = make_context(environment_root, runner, "uv", {"npm"})

    with pytest.raises(UpdateError) as error:
        resolve_update_plan(context)

    message = str(error.value)
    assert "postinstall" in message
    assert "npm update --global crewplane" in message
    assert "npm rebuild --global crewplane" in message
    assert [command for command, _kwargs in runner.calls] == [
        ("npm", "root", "--global")
    ]


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            InstalledMetadata(installer="pip", editable=False, direct_source=False),
            "(?i)directly in a Python",
        ),
        (
            InstalledMetadata(installer="uv", editable=False, direct_source=False),
            "(?i)uv-managed Python",
        ),
        (
            InstalledMetadata(installer="pip", editable=True, direct_source=True),
            "(?i)editable",
        ),
        (
            InstalledMetadata(installer="uv", editable=False, direct_source=True),
            "(?i)direct URL|local source",
        ),
        (
            InstalledMetadata(installer=None, editable=False, direct_source=False),
            "(?i)could not confidently identify",
        ),
    ],
)
def test_unmanaged_installations_receive_manual_guidance(
    tmp_path: Path,
    metadata: InstalledMetadata,
    message: str,
) -> None:
    runner = ProbeRunner({})
    context = make_context(tmp_path / "project" / ".venv", runner, None, set())
    context = UpdateContext(
        package_name=context.package_name,
        python_executable=context.python_executable,
        environment_root=context.environment_root,
        metadata=metadata,
        current_version=context.current_version,
        executable_lookup=context.executable_lookup,
        command_runner=context.command_runner,
    )

    with pytest.raises(UpdateError, match=message):
        resolve_update_plan(context)

    assert runner.calls == []


def test_ambiguous_manager_ownership_is_rejected(
    tmp_path: Path,
) -> None:
    formula_prefix = tmp_path / "Cellar" / "crewplane" / "0.1.1"
    pipx_home = formula_prefix / "pipx"
    environment_root = pipx_home / "venvs" / "crewplane"
    write_pipx_metadata(environment_root)
    runner = ProbeRunner(
        {
            ("pipx", "environment", "--value", "PIPX_HOME"): f"{pipx_home}\n",
            ("brew", "--prefix", "crewplane"): f"{formula_prefix}\n",
        }
    )
    context = make_context(environment_root, runner, "pip", {"pipx", "brew"})

    with pytest.raises(UpdateError, match="conflicting"):
        resolve_update_plan(context)


@pytest.mark.parametrize("owner", ["uv", "pipx", "brew", "npm"])
def test_manager_on_path_with_a_different_root_is_rejected(
    tmp_path: Path,
    owner: str,
) -> None:
    if owner == "uv":
        environment_root = tmp_path / "uv-a" / "tools" / "crewplane"
        write_uv_receipt(environment_root)
        outputs = {("uv", "tool", "dir"): f"{tmp_path / 'uv-b' / 'tools'}\n"}
        installer = "uv"
    elif owner == "pipx":
        environment_root = tmp_path / "pipx-a" / "venvs" / "crewplane"
        write_pipx_metadata(environment_root)
        outputs = {
            ("pipx", "environment", "--value", "PIPX_HOME"): (
                f"{tmp_path / 'pipx-b'}\n"
            )
        }
        installer = "pip"
    elif owner == "brew":
        environment_root = (
            tmp_path / "brew-a" / "Cellar" / "crewplane" / "0.1.1" / "libexec"
        )
        outputs = {
            ("brew", "--prefix", "crewplane"): (
                f"{tmp_path / 'brew-b' / 'Cellar' / 'crewplane' / '0.1.1'}\n"
            )
        }
        installer = None
    else:
        npm_root = tmp_path / "npm-a" / "node_modules"
        package_root = npm_root / "crewplane"
        environment_root = package_root / ".venv"
        write_npm_package(package_root)
        outputs = {
            ("npm", "root", "--global"): (f"{tmp_path / 'npm-b' / 'node_modules'}\n")
        }
        installer = "uv"

    runner = ProbeRunner(outputs)
    context = make_context(environment_root, runner, installer, {owner})

    with pytest.raises(UpdateError):
        resolve_update_plan(context)


def test_linked_global_npm_package_is_rejected(tmp_path: Path) -> None:
    npm_root = tmp_path / "lib" / "node_modules"
    source_root = tmp_path / "source"
    source_root.mkdir()
    write_npm_package(source_root)
    npm_root.mkdir(parents=True)
    package_root = npm_root / "crewplane"
    package_root.symlink_to(source_root, target_is_directory=True)
    environment_root = package_root / ".venv"
    runner = ProbeRunner({("npm", "root", "--global"): f"{npm_root}\n"})
    context = make_context(environment_root, runner, "uv", {"npm"})

    with pytest.raises(UpdateError, match="linked|symlink"):
        resolve_update_plan(context)
