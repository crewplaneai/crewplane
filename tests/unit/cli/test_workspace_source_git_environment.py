from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.helpers import isolated_git as _isolated_git_support
from tests.helpers.isolated_git import (
    configure_isolated_git_environment,
    require_git,
    run_git,
)

isolated_git = _isolated_git_support.isolated_git


pytestmark = pytest.mark.usefixtures("isolated_git")


@pytest.mark.parametrize(
    ("required", "expected_outcome"),
    [
        pytest.param(False, pytest.skip.Exception, id="optional-skips"),
        pytest.param(True, pytest.fail.Exception, id="required-fails"),
    ],
)
def test_workspace_git_capability_respects_required_mode(
    monkeypatch: pytest.MonkeyPatch,
    required: bool,
    expected_outcome: type[BaseException],
) -> None:
    def no_git(name: str, path: str | None = None) -> None:
        del name, path

    monkeypatch.setattr(
        _isolated_git_support.shutil,
        "which",
        no_git,
    )

    with pytest.raises(expected_outcome, match="git is unavailable"):
        require_git(dict(os.environ), required)


def test_workspace_git_floor_bin_forces_required_mode(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="git is unavailable"):
        require_git(
            {"PATH": ""},
            required=False,
            expected_floor_bin=(tmp_path / "floor" / "bin").as_posix(),
        )


def test_workspace_git_floor_bin_accepts_exact_executable_and_version(
    tmp_path: Path,
) -> None:
    floor_bin = tmp_path / "floor" / "bin"
    expected_git = _write_fake_git(floor_bin, "git version 2.34.1")

    git = require_git(
        {"PATH": floor_bin.as_posix()},
        required=True,
        expected_floor_bin=floor_bin.as_posix(),
    )

    assert git.executable == expected_git.resolve()


def test_workspace_git_floor_bin_rejects_unexpected_executable(
    tmp_path: Path,
) -> None:
    floor_bin = tmp_path / "floor" / "bin"
    actual_bin = tmp_path / "other" / "bin"
    _write_fake_git(floor_bin, "git version 2.34.1")
    _write_fake_git(actual_bin, "git version 2.34.1")

    with pytest.raises(pytest.fail.Exception, match="unexpected executable"):
        require_git(
            {"PATH": f"{actual_bin.as_posix()}{os.pathsep}{floor_bin.as_posix()}"},
            required=True,
            expected_floor_bin=floor_bin.as_posix(),
        )


def test_workspace_git_floor_bin_rejects_unexpected_version(
    tmp_path: Path,
) -> None:
    floor_bin = tmp_path / "floor" / "bin"
    _write_fake_git(floor_bin, "git version 2.35.0")

    with pytest.raises(pytest.fail.Exception, match="unexpected version"):
        require_git(
            {"PATH": floor_bin.as_posix()},
            required=True,
            expected_floor_bin=floor_bin.as_posix(),
        )


def _write_fake_git(bin_dir: Path, version_text: str) -> Path:
    bin_dir.mkdir(parents=True)
    executable = bin_dir / "git"
    executable.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{version_text}'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_workspace_git_environment_isolated_from_ambient_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/ambient/repository")
    monkeypatch.setenv("GIT_ASKPASS", "/ambient/askpass")
    monkeypatch.setenv("GIT_FLOOR_BIN", "/ambient/git-floor/bin")
    monkeypatch.setenv("SSH_ASKPASS", "/ambient/ssh-askpass")

    isolated_root = tmp_path / "second-environment"
    isolated_root.mkdir()
    environment = configure_isolated_git_environment(monkeypatch, isolated_root)

    assert "GIT_DIR" not in environment
    assert "GIT_ASKPASS" not in environment
    assert "GIT_FLOOR_BIN" not in environment
    assert "SSH_ASKPASS" not in environment
    assert environment["HOME"] == (isolated_root / "home").as_posix()
    assert environment["XDG_CONFIG_HOME"] == (isolated_root / "xdg-config").as_posix()
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert (
        environment["GIT_CONFIG_GLOBAL"]
        == (isolated_root / "empty-gitconfig").as_posix()
    )
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_ALLOW_PROTOCOL"] == "file"
    assert environment["LC_ALL"] == "C"
    assert environment["TZ"] == "UTC"


def test_workspace_git_failure_diagnostics_redact_credentials(
    tmp_path: Path,
) -> None:
    with pytest.raises(AssertionError) as captured:
        run_git(
            tmp_path,
            "not-a-command",
            "https://user:password@example.invalid/repository",
            "token=private-value",
            "authToken=camel-secret",
            "credential.helper=helper-secret",
            "-c",
            "http.extraHeader=Authorization: Bearer header-secret",
            "--password",
            "option-secret",
            "--_authToken=generic-secret",
            "--otp",
        )

    diagnostic = str(captured.value)
    for secret in (
        "user:password",
        "private-value",
        "camel-secret",
        "helper-secret",
        "header-secret",
        "option-secret",
        "generic-secret",
    ):
        assert secret not in diagnostic
    assert "https://<redacted>@example.invalid/repository" in diagnostic
    assert "token=<redacted>" in diagnostic
    assert "authToken=<redacted>" in diagnostic
    assert "credential.helper=<redacted>" in diagnostic
    assert "Authorization: <redacted>" in diagnostic
    assert "--password '<redacted>'" in diagnostic
    assert "--_authToken=<redacted>" in diagnostic
    assert "--otp '<missing-value>'" in diagnostic
    assert "exit status" in diagnostic
    assert "stdout:" in diagnostic
    assert "stderr:" in diagnostic


def test_workspace_git_failure_diagnostics_redact_output_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                "printf '%s\\n' 'stdout token=stdout-secret credential.helper=stdout-helper'",
                "printf '%s\\n' 'Authorization: Bearer stdout-header'",
                "printf '%s\\n' 'stderr https://user:stderr-password@example.invalid/repository authToken=stderr-token' >&2",
                "exit 1",
            )
        ),
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin.as_posix()}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(AssertionError) as captured:
        run_git(tmp_path, "status")

    diagnostic = str(captured.value)
    for secret in (
        "stdout-secret",
        "stdout-helper",
        "stdout-header",
        "stderr-password",
        "stderr-token",
    ):
        assert secret not in diagnostic
    assert "token=<redacted>" in diagnostic
    assert "credential.helper=<redacted>" in diagnostic
    assert "Authorization: <redacted>" in diagnostic
    assert "https://<redacted>@example.invalid/repository" in diagnostic
    assert "authToken=<redacted>" in diagnostic


def test_workspace_git_ignores_ambient_global_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient_hooks = tmp_path / "ambient-hooks"
    ambient_hooks.mkdir()
    pre_commit = ambient_hooks / "pre-commit"
    pre_commit.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    pre_commit.chmod(0o755)
    ambient_config = tmp_path / "ambient-gitconfig"
    ambient_config.write_text(
        "\n".join(
            (
                "[init]",
                "    defaultBranch = hostile",
                "[commit]",
                "    gpgSign = true",
                "[core]",
                f"    hooksPath = {ambient_hooks.as_posix()}",
                "    fsmonitor = true",
                "[user]",
                "    name = Ambient User",
                "    email = ambient@example.invalid",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", ambient_config.as_posix())
    isolated_root = tmp_path / "isolated-environment"
    isolated_root.mkdir()
    environment = configure_isolated_git_environment(monkeypatch, isolated_root)
    isolated_git = require_git(environment, required=True)
    repository = tmp_path / "repository"
    repository.mkdir()

    isolated_git.run_text(repository, "init")
    (repository / "README.md").write_text("ready\n", encoding="utf-8")
    isolated_git.run_text(repository, "add", "README.md")
    isolated_git.run_text(repository, "commit", "-m", "initial")

    assert (
        isolated_git.run_text(
            repository,
            "symbolic-ref",
            "--short",
            "HEAD",
        )
        == "main"
    )
    assert (
        isolated_git.run_text(
            repository,
            "log",
            "-1",
            "--format=%an <%ae>",
        )
        == "Crewplane Test <crewplane-test@example.invalid>"
    )
    fsmonitor = isolated_git.run(
        repository,
        "config",
        "--get",
        "core.fsmonitor",
        check=False,
    )
    assert fsmonitor.returncode == 1
