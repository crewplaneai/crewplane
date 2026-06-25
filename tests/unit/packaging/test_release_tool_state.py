from __future__ import annotations

# ruff: noqa: E402, I001

import json
from pathlib import Path
import sys

_LOCAL_TEST_DIR = Path(__file__).resolve().parent
if str(_LOCAL_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_TEST_DIR))

import pytest

from scripts.release import build, state
from test_release_tool_fixtures import (
    FakeRunner,
    append_uv_lock_package,
    constant,
    write_minimal_repo,
)


def test_release_version_maps_python_and_npm_versions() -> None:
    cases = {
        "1.2.3-alpha.4": ("1.2.3a4", "1.2.3-alpha.4"),
        "1.2.3b5": ("1.2.3b5", "1.2.3-beta.5"),
        "1.2.3rc6": ("1.2.3rc6", "1.2.3-rc.6"),
        "1.2.3.dev7": ("1.2.3.dev7", "1.2.3-dev.7"),
        "1.2.3.post8": ("1.2.3.post8", "1.2.3-post.8"),
    }
    for project_version, expected in cases.items():
        release = state.ReleaseVersion.from_project(project_version)
        assert (release.python, release.npm) == expected

    with pytest.raises(state.ReleaseError, match="local version identifiers"):
        state.ReleaseVersion.from_project("1.2.3+local")


def test_command_runner_reports_stdout_and_stderr_on_failure(tmp_path: Path) -> None:
    runner = state.CommandRunner()

    with pytest.raises(state.ReleaseError) as caught:
        runner.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "print('visible stdout'); "
                    "print('visible stderr', file=sys.stderr); "
                    "raise SystemExit(7)"
                ),
            ],
            cwd=tmp_path,
        )

    message = str(caught.value)
    assert "stdout:\nvisible stdout" in message
    assert "stderr:\nvisible stderr" in message


def test_metadata_sync_refreshes_and_reads_back_generated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_repo(tmp_path)
    monkeypatch.setattr(state.state_sync, "command_exists", lambda name: name == "uv")

    context = state.read_release_context(tmp_path)
    runner = FakeRunner()
    state.sync_generated_metadata(context, runner)

    assert ("uv", "lock") in runner.commands
    package = json.loads((tmp_path / "packaging" / "npm" / "package.json").read_text())
    assert package["version"] == "1.2.3-alpha.4"
    assert package["crewplane"]["pythonPackageVersion"] == "1.2.3-alpha.4"
    assert f'CREWPLANE_VERSION="${{CREWPLANE_VERSION:-{context.version.project}}}"' in (
        tmp_path / "install.sh"
    ).read_text(encoding="utf-8")
    assert "crewplane@alpha" not in (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "crewplane/main/install.sh" not in (
        tmp_path / "docs" / "getting-started" / "installation.md"
    ).read_text(encoding="utf-8")
    formula = (
        tmp_path / "packaging" / "homebrew" / "Formula" / "crewplane.rb"
    ).read_text(encoding="utf-8")
    assert 'version "1.2.3-alpha.4"' in formula
    assert 'branch: "master"' in formula
    assert not state.verify_generated_metadata(context, None)

    state.sync_generated_metadata(context, runner)
    assert not state.verify_generated_metadata(context, None)


def test_sync_updates_all_formula_resource_pins_from_uv_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_repo(tmp_path)
    formula = (
        tmp_path / "packaging" / "homebrew" / "Formula" / "crewplane.rb"
    ).read_text(encoding="utf-8")
    formula = formula.replace(
        '  resource "typer" do\n'
        '    url "https://example.com/typer-0.0.0.tar.gz"\n'
        '    sha256 "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"\n'
        "  end",
        '  resource "typer" do\n'
        '    url "https://example.com/typer-0.0.0.tar.gz"\n'
        '    sha256 "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"\n'
        "  end\n"
        '  resource "mdurl" do\n'
        '    url "https://example.com/mdurl-stale.tar.gz"\n'
        '    sha256 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
        "  end",
    )
    (tmp_path / "packaging" / "homebrew" / "Formula" / "crewplane.rb").write_text(
        formula, encoding="utf-8"
    )
    append_uv_lock_package(
        tmp_path,
        "mdurl",
        "https://example.com/mdurl-new.tar.gz",
        "d" * 64,
    )

    monkeypatch.setattr(state.state_sync, "command_exists", lambda name: name == "uv")

    original_refresh_uv_lock = state.state_sync.refresh_uv_lock

    def refresh_with_mdurl(context: state.ReleaseContext, runner: FakeRunner) -> None:
        original_refresh_uv_lock(context, runner)
        append_uv_lock_package(
            tmp_path,
            "mdurl",
            "https://example.com/mdurl-new.tar.gz",
            "d" * 64,
        )

    monkeypatch.setattr(state.state_sync, "refresh_uv_lock", refresh_with_mdurl)

    context = state.read_release_context(tmp_path)
    runner = FakeRunner()
    state.sync_generated_metadata(context, runner)

    formula = (
        tmp_path / "packaging" / "homebrew" / "Formula" / "crewplane.rb"
    ).read_text(encoding="utf-8")
    assert "https://example.com/mdurl-new.tar.gz" in formula


def test_formula_resource_sync_cleans_generated_blank_lines(
    tmp_path: Path,
) -> None:
    write_minimal_repo(tmp_path)
    formula_path = tmp_path / "packaging" / "homebrew" / "Formula" / "crewplane.rb"
    formula = formula_path.read_text(encoding="utf-8")
    formula = formula.replace(
        '  resource "typer" do\n'
        '    url "https://example.com/typer-0.0.0.tar.gz"\n'
        '    sha256 "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"\n'
        "  end",
        '  resource "typer" do\n'
        "\n"
        '    url "https://example.com/typer-0.0.0.tar.gz"\n'
        "\n"
        '    sha256 "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"\n'
        "\n"
        "  end",
    )
    formula_path.write_text(formula, encoding="utf-8")

    context = state.read_release_context(tmp_path)
    state.sync_homebrew_formula_metadata(context, "1" * 64)

    formula = formula_path.read_text(encoding="utf-8")
    assert (
        '  resource "typer" do\n'
        '    url "https://example.com/typer-0.0.0.tar.gz"\n'
        '    sha256 "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"\n'
        "  end"
    ) in formula


def test_metadata_sync_fails_when_replacement_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_repo(tmp_path)
    (tmp_path / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(state.state_sync, "command_exists", lambda name: name == "uv")
    context = state.read_release_context(tmp_path)

    with pytest.raises(state.ReleaseError, match="expected exactly one replacement"):
        state.sync_generated_metadata(context, FakeRunner())


def test_prepare_refuses_existing_remote_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    existing_pypi = state.PypiRelease(True, context.version.python, {})
    missing_npm = state.NpmRelease(False, "", "", "", "", "", "", "", "")
    monkeypatch.setattr(
        build, "query_registry_state", constant((existing_pypi, missing_npm))
    )

    with pytest.raises(state.ReleaseError, match="already exists on PyPI"):
        build.prepare_release(tmp_path, FakeRunner())
