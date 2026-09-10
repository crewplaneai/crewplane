from __future__ import annotations

import json
import os
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, PathDistribution
from pathlib import Path

import pytest

from crewplane.cli.update import runner
from crewplane.cli.update.detection import resolve_update_plan
from crewplane.cli.update.types import UpdateError
from tests.unit.cli.update_helpers import FailedCommands, update_context


@pytest.mark.parametrize(
    ("filename", "contents", "message"),
    [
        ("METADATA", b"Version: 1.0\n", "does not declare a project name"),
        ("direct_url.json", b"{", "not valid JSON"),
        ("direct_url.json", b"[]", "must be a JSON object"),
        ("INSTALLER", b"\xff", "not valid text"),
    ],
)
def test_default_update_context_rejects_corrupt_installed_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    contents: bytes,
    message: str,
) -> None:
    metadata = tmp_path / "crewplane-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Name: crewplane\nVersion: 1.0\n", encoding="utf-8"
    )
    (metadata / filename).write_bytes(contents)

    def installed_distribution(name: str) -> PathDistribution:
        assert name == "crewplane"
        return PathDistribution(metadata)

    monkeypatch.setattr(runner, "distribution", installed_distribution)

    with pytest.raises(UpdateError, match=message):
        runner.default_update_context()


def test_default_update_context_preserves_missing_distribution_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = PackageNotFoundError("crewplane")

    def unavailable_distribution(name: str) -> PathDistribution:
        assert name == "crewplane"
        raise failure

    monkeypatch.setattr(runner, "distribution", unavailable_distribution)

    with pytest.raises(UpdateError, match="metadata could not be found") as caught:
        runner.default_update_context()

    assert caught.value.__cause__ is failure


def test_default_update_context_preserves_distribution_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = tmp_path / "crewplane-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Name: crewplane\nVersion: 1.0\n", encoding="utf-8"
    )
    failure = PermissionError("permission revoked")

    class InaccessibleInstallerDistribution(PathDistribution):
        def read_text(self, filename: str | os.PathLike[str]) -> str | None:
            if filename == "INSTALLER":
                raise failure
            return super().read_text(filename)

    def installed_distribution(name: str) -> PathDistribution:
        assert name == "crewplane"
        return InaccessibleInstallerDistribution(metadata)

    monkeypatch.setattr(runner, "distribution", installed_distribution)

    with pytest.raises(UpdateError, match="INSTALLER.*could not be read") as caught:
        runner.default_update_context()

    assert caught.value.__cause__ is failure


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"main_package": []},
        {"main_package": {"package": "another-package"}},
        {"main_package": {"package": "crewplane", "suffix": 1}},
        {"main_package": {"package": "crewplane", "suffix": "/unsafe"}},
        {"main_package": {"package": "crewplane", "suffix": "-other"}},
        {"main_package": {"package": "crewplane"}, "environment": "another-package"},
    ],
)
def test_pipx_owner_requires_consistent_main_package_metadata(
    tmp_path: Path, metadata: dict[str, object]
) -> None:
    commands = FailedCommands({})
    context = update_context(tmp_path, commands)
    (context.environment_root / "uv-receipt.toml").unlink()
    (context.environment_root / "pipx_metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )

    with pytest.raises(UpdateError, match="does not identify this environment"):
        resolve_update_plan(context)

    assert commands.calls == []


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (b"{", "not valid JSON"),
        (b"[]", "must be a JSON object"),
        (b"\xff", "could not be read"),
    ],
)
def test_pipx_owner_rejects_unreadable_and_malformed_receipts(
    tmp_path: Path, contents: bytes, message: str
) -> None:
    commands = FailedCommands({})
    context = update_context(tmp_path, commands)
    (context.environment_root / "uv-receipt.toml").unlink()
    (context.environment_root / "pipx_metadata.json").write_bytes(contents)

    with pytest.raises(UpdateError, match=message):
        resolve_update_plan(context)

    assert commands.calls == []


@pytest.mark.parametrize(
    ("output", "message"),
    [
        pytest.param("", "ownership probe returned no path", id="empty-output"),
        pytest.param("\n", "ownership probe returned no path", id="blank-output"),
        pytest.param(
            "/tools/one\n/tools/two", "returned an ambiguous path", id="multiple-paths"
        ),
    ],
)
def test_owner_probe_requires_one_nonempty_path(
    tmp_path: Path, output: str, message: str
) -> None:
    commands = FailedCommands({})
    context = update_context(tmp_path, commands)
    probe = ("uv", "tool", "dir")
    commands.responses[probe] = output

    with pytest.raises(UpdateError, match=message):
        resolve_update_plan(context)

    assert commands.calls == [probe]


def test_missing_owner_executable_does_not_start_a_probe(tmp_path: Path) -> None:
    commands = FailedCommands({})
    context = update_context(tmp_path, commands)
    executables: dict[str, str] = {}
    context = replace(context, executable_lookup=executables.get)

    with pytest.raises(UpdateError, match="not found on PATH"):
        resolve_update_plan(context)

    assert commands.calls == []


def test_owner_probe_with_symlink_loop_fails_closed(tmp_path: Path) -> None:
    commands = FailedCommands({})
    context = update_context(tmp_path, commands)
    probe = ("uv", "tool", "dir")
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    commands.responses[probe] = str(loop)

    with pytest.raises(UpdateError, match="different Crewplane tool environment"):
        resolve_update_plan(context)

    assert commands.calls == [probe]
