"""Execution controls and test directory ownership are deliberate inputs."""

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import run_tests
from tests.helpers.execution_environment import (
    build_execution_environment,
    directory_environment,
    remove_execution_directory,
)
from tests.helpers.isolated_git import configure_isolated_git_environment


def test_builder_excludes_ambient_settings_and_preserves_launch_requirements(tmp_path):
    parent = {
        "SYSTEMROOT": "platform-root",
        "PATH": "ambient-tools",
        "OLLAMA_HOST": "http://ambient.invalid",
        "OPENAI_API_KEY": "synthetic-secret",
        "PYTHONPATH": "ambient-imports",
        "PYTHONHOME": "ambient-python",
        "PYTEST_ADDOPTS": "--collect-only",
        "PYTEST_PLUGINS": "ambient_plugin",
        "COVERAGE_PROCESS_CONFIG": "ambient-coverage",
        "XDG_CACHE_HOME": "external-cache",
    }
    environment = build_execution_environment(tmp_path, "chosen-tools", parent=parent)
    tool_path = environment["PATH"]
    system_root = environment["SYSTEMROOT"]
    assert tool_path == "chosen-tools"
    assert system_root == "platform-root"
    for key in (
        "OLLAMA_HOST",
        "OPENAI_API_KEY",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "COVERAGE_PROCESS_CONFIG",
    ):
        present = key in environment
        assert not present, key
    for key in (
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
    ):
        directory = Path(environment[key])
        assert directory.is_relative_to(tmp_path)
        assert directory.is_dir()
    locale = environment["LC_ALL"]
    timezone = environment["TZ"]
    terminal = environment["TERM"]
    assert locale == "C"
    assert timezone == "UTC"
    assert terminal == "dumb"


def test_explicit_scenario_values_reach_child_environment(tmp_path):
    environment = build_execution_environment(
        tmp_path,
        "tools",
        {"OLLAMA_HOST": "http://scenario.invalid:11434"},
        {"OLLAMA_HOST": "http://ambient.invalid:11434"},
    )
    host = environment["OLLAMA_HOST"]
    assert host == "http://scenario.invalid:11434"


def test_launcher_preserves_required_git_and_prepends_floor(tmp_path, monkeypatch):
    floor = str(tmp_path / "git-floor")
    monkeypatch.setenv("PATH", "ordinary-tools")
    monkeypatch.setenv("GIT_FLOOR_BIN", floor)
    monkeypatch.setenv("CREWPLANE_REQUIRED_GIT", "1")
    args = run_tests.parse_args(["--profile", "local", "--hash-seed", "42"])
    environment = run_tests.environment_for(args, tmp_path, tmp_path / "environment")
    first_tool_directory = environment["PATH"].split(os.pathsep)[0]
    git_floor = environment["GIT_FLOOR_BIN"]
    required_git = environment["CREWPLANE_REQUIRED_GIT"]
    assert first_tool_directory == floor
    assert git_floor == floor
    assert required_git == "1"


def test_git_setup_uses_common_locations_and_restores_parent(tmp_path, monkeypatch):
    external = tmp_path / "external-cache"
    external.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(external))
    initial_home = os.environ.get("HOME")
    root = tmp_path / "owned"
    with monkeypatch.context() as scoped:
        configure_isolated_git_environment(scoped, root)
        home = os.environ["HOME"]
        expected_home = directory_environment(root)["HOME"]
        assert home == expected_home
        cache = Path(os.environ["XDG_CACHE_HOME"])
        assert cache == root / "xdg-cache"
        (cache / "receipt").write_text("owned", encoding="utf-8")
    restored_home = os.environ.get("HOME")
    restored_cache = os.environ["XDG_CACHE_HOME"]
    assert restored_home == initial_home
    assert restored_cache == str(external)
    assert not list(external.iterdir())


@pytest.mark.parametrize("iteration", range(2))
def test_function_scoped_home_and_cache_are_fresh(isolated_test_environment, iteration):
    for key in ("HOME", "XDG_CACHE_HOME"):
        directory = Path(os.environ[key])
        assert directory.is_relative_to(isolated_test_environment)
        marker = directory / "per-test-write"
        assert not marker.exists()
        marker.write_text(str(iteration), encoding="utf-8")


def test_per_test_environments_do_not_expand_the_numbered_directory_namespace(
    tmp_path_factory,
):
    from tests import conftest

    basetemp = tmp_path_factory.getbasetemp()
    original_entries = set(basetemp.iterdir())
    roots = []
    environment_scope = contextmanager(conftest.isolated_test_environment.__wrapped__)
    for iteration in range(8):
        with environment_scope(tmp_path_factory) as root:
            (root / "receipt").write_text(str(iteration), encoding="utf-8")
            roots.append(root)
            assert Path(os.environ["HOME"]).is_relative_to(root)

    assert len(set(roots)) == 8
    assert [(root / "receipt").read_text() for root in roots] == list(
        map(str, range(8))
    )
    assert all(root.is_dir() and root.is_relative_to(basetemp) for root in roots)
    assert len(set(basetemp.iterdir()) - original_entries) <= 1


def test_dots_installation_retains_acquisition_settings_but_pytest_isolated(
    tmp_path, monkeypatch
):
    from scripts.test_process import Deadline, ProcessOutcome

    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://packages.example.invalid/simple")
    monkeypatch.setenv("OLLAMA_HOST", "http://ambient.invalid:11434")
    observed = []

    def run(command, cwd, environment, log, timeout, shutdown_timeout):
        del command, cwd, log, timeout, shutdown_timeout
        observed.append(
            (environment.get("UV_DEFAULT_INDEX"), environment.get("OLLAMA_HOST"))
        )
        return ProcessOutcome(0, 0)

    monkeypatch.setattr(run_tests, "run_logged", run)
    args = run_tests.parse_args(["--suite", "dots", "--hash-seed", "42"])
    run_tests.run_dots(
        args, tmp_path / "dots", Deadline.after(30), tmp_path / "environment"
    )
    assert observed == [
        ("https://packages.example.invalid/simple", "http://ambient.invalid:11434"),
        (None, None),
    ]


@pytest.mark.parametrize("read_only", [False, True])
def test_execution_cleanup_removes_long_artifact_paths(tmp_path, read_only):
    from tests.helpers.platforms import extended_file_test_root

    root = tmp_path / "execution"
    artifact = extended_file_test_root(root)
    while len(str(artifact)) < 300:
        artifact /= "nested-artifact-directory"
    artifact.mkdir(parents=True)
    receipt = artifact / "receipt.txt"
    receipt.write_bytes(b"owned execution output")
    if read_only:
        receipt.chmod(stat.S_IREAD)

    try:
        remove_execution_directory(root)
    finally:
        if receipt.exists():
            receipt.chmod(stat.S_IREAD | stat.S_IWRITE)

    assert not root.exists()
    assert not receipt.exists()


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows file sharing semantics")
@pytest.mark.parametrize("read_only", [False, True])
def test_execution_cleanup_reports_locked_files(tmp_path, read_only):
    root = tmp_path / "execution"
    root.mkdir()
    receipt = root / "locked.txt"
    receipt.write_bytes(b"retained evidence")
    if read_only:
        receipt.chmod(stat.S_IREAD)

    try:
        with receipt.open("rb"), pytest.raises(PermissionError):
            remove_execution_directory(root)
        assert receipt.read_bytes() == b"retained evidence"
    finally:
        if receipt.exists():
            receipt.chmod(stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.parametrize("test_exit", [0, 7])
def test_cleanup_preserves_test_outcome_and_reports_its_own_failure(
    tmp_path, monkeypatch, test_exit
):
    from scripts.test_process import ProcessOutcome

    def run(command, cwd, environment, log, timeout, shutdown_timeout):
        del command, cwd, environment, timeout, shutdown_timeout
        log.write_text("original test outcome", encoding="utf-8")
        return ProcessOutcome(test_exit, 0)

    def metadata(args, command, environment, deadline):
        del args, command, deadline
        return {
            "reproduce": "test-owned command",
            "environment_root": environment["CREWPLANE_TEST_ENV_ROOT"],
        }

    def fail_cleanup(path):
        del path
        raise PermissionError("owned directory locked")

    monkeypatch.setattr(run_tests, "ROOT", tmp_path)
    monkeypatch.setattr(run_tests, "run_logged", run)
    monkeypatch.setattr(run_tests, "metadata", metadata)
    monkeypatch.setattr(
        run_tests.execution_environment, "remove_execution_directory", fail_cleanup
    )
    result = run_tests.main(["--profile", "nightly", "--hash-seed", "42"])
    assert result == (test_exit or 1)
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert outcome["pytest"]["returncode"] == test_exit
    assert outcome["exit_code"] == result
    assert (diagnostics / "pytest.log").read_text() == "original test outcome"
    metadata = json.loads((diagnostics / "metadata.json").read_text())
    assert Path(metadata["environment_root"]).exists()
    if test_exit == 0:
        assert outcome["cleanup_error"] == "PermissionError: owned directory locked"
    else:
        assert "cleanup_error" not in outcome
