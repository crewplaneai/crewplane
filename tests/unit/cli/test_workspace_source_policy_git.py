from __future__ import annotations

import os
from pathlib import Path

import pytest

from crewplane.cli.run.workspace import source_policy as policy
from crewplane.cli.run.workspace.git_source import discover_git_context
from crewplane.cli.run.workspace.source_types import WorkspacePolicyBuilder
from crewplane.core.config import Settings
from tests.helpers import isolated_git as _isolated_git_support
from tests.helpers.isolated_git import (
    IsolatedGit,
    configure_isolated_git_environment,
    require_git,
    run_git,
    run_git_text,
)
from tests.helpers.workspace_source_policy import (
    git_source_context,
    workspace_source_config,
    workspace_source_workflow,
)

isolated_git = _isolated_git_support.isolated_git

pytestmark = pytest.mark.usefixtures("isolated_git")


def test_workspace_source_policy_ignores_untracked_attributes_with_tracked_only(
    tmp_path: Path,
) -> None:
    run_git_text(tmp_path, "init")
    run_git_text(tmp_path, "config", "user.name", "Crewplane Test")
    run_git_text(tmp_path, "config", "user.email", "crewplane-test@example.invalid")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("guide\n", encoding="utf-8")
    run_git_text(tmp_path, "add", "docs/guide.md")
    run_git_text(tmp_path, "commit", "-m", "initial")
    (docs_dir / ".gitattributes").write_text("*.md text=auto\n", encoding="utf-8")
    config = workspace_source_config().model_copy(
        update={
            "settings": Settings(
                workspace={"enabled": True, "clean_start": "tracked_only"}
            )
        }
    )

    result = policy.collect_workspace_source_policy(
        config=config,
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.errors == ()
    assert result.source_snapshot is not None
    assert result.source_snapshot.clean_start == "tracked_only"
    assert any("tracked_only excluded" in warning for warning in result.warnings)
    assert any(
        "Required by logical worktrees: primary" in warning
        for warning in result.warnings
    )


def test_workspace_source_policy_non_git_error_has_remediation(
    tmp_path: Path,
) -> None:
    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.source_snapshot is None
    assert len(result.errors) == 1
    assert "requires a Git repository with a valid HEAD commit" in result.errors[0]
    assert "settings.workspace.enabled: false" in result.errors[0]


def test_discover_git_context_resolves_common_dir_from_project_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    nested_root = project_root / "app"
    nested_root.mkdir(parents=True)
    run_git_text(project_root, "init")
    run_git_text(project_root, "config", "user.name", "Crewplane Test")
    run_git_text(project_root, "config", "user.email", "crewplane-test@example.invalid")
    (nested_root / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(project_root, "add", "app/README.md")
    run_git_text(project_root, "commit", "-m", "initial")
    builder = WorkspacePolicyBuilder()

    context = discover_git_context(nested_root, builder)

    assert builder.errors == []
    assert context is not None
    assert context.git_top_level == project_root.resolve()
    assert context.project_root_relative_path == "app"
    assert context.common_git_dir == (project_root / ".git").resolve()


def test_git_source_checks_reports_filesystem_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def noop(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def local_config(*args: object, **kwargs: object) -> dict[str, tuple[str, ...]]:
        del args, kwargs
        return {}

    def fail_policy_read(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PermissionError("policy file denied")

    monkeypatch.setattr(policy, "validate_cache_root", noop)
    monkeypatch.setattr(policy, "validate_unsupported_repo_state", noop)
    monkeypatch.setattr(policy, "validate_local_git_config", local_config)
    monkeypatch.setattr(policy, "validate_local_policy_files", fail_policy_read)
    builder = WorkspacePolicyBuilder()

    local_config_policy, filesystem_capabilities = policy.collect_git_source_checks(
        Settings(workspace={"enabled": True}),
        tmp_path,
        tmp_path / ".crewplane",
        git_source_context(tmp_path),
        estimate_full_repository=False,
        logical_worktree_names=(),
        real_execution=False,
        builder=builder,
    )

    assert local_config_policy == {}
    assert filesystem_capabilities == {}
    assert len(builder.errors) == 1
    assert "Git source inspection failed" in builder.errors[0]
    assert "policy file denied" in builder.errors[0]


def test_workspace_source_policy_rejects_lfs_attributes_with_remediation(
    tmp_path: Path,
) -> None:
    _create_clean_repo(tmp_path)
    (tmp_path / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n",
        encoding="utf-8",
    )
    (tmp_path / "asset.bin").write_bytes(b"binary\n")
    run_git_text(tmp_path, "add", ".gitattributes", "asset.bin")
    run_git_text(tmp_path, "commit", "-m", "lfs attributes")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert any(
        "Git LFS filter=lfs" in error
        and "asset.bin" in error
        and "settings.workspace.enabled: false" in error
        for error in result.errors
    )


def test_workspace_source_policy_rejects_custom_filter_attributes_with_remediation(
    tmp_path: Path,
) -> None:
    _create_clean_repo(tmp_path)
    (tmp_path / ".gitattributes").write_text(
        "*.secret filter=crypt\n", encoding="utf-8"
    )
    (tmp_path / "credentials.secret").write_text("secret\n", encoding="utf-8")
    run_git_text(tmp_path, "add", ".gitattributes", "credentials.secret")
    run_git_text(tmp_path, "commit", "-m", "custom filter attributes")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert any(
        "custom Git filter=crypt" in error
        and "credentials.secret" in error
        and "settings.workspace.enabled: false" in error
        for error in result.errors
    )


def test_workspace_source_policy_rejects_text_normalization_with_remediation(
    tmp_path: Path,
) -> None:
    _create_clean_repo(tmp_path)
    (tmp_path / ".gitattributes").write_text("*.md text=auto\n", encoding="utf-8")
    run_git_text(tmp_path, "add", ".gitattributes")
    run_git_text(tmp_path, "commit", "-m", "text attributes")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert any(
        "text normalization text=auto" in error
        and "README.md" in error
        and "settings.workspace.enabled: false" in error
        for error in result.errors
    )


def test_workspace_source_policy_clean_start_names_logical_worktree(
    tmp_path: Path,
) -> None:
    _create_clean_repo(tmp_path)
    (tmp_path / "README.md").write_text("dirty\n", encoding="utf-8")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert any(
        "tracked files have staged or unstaged changes" in error
        and "Required by logical worktrees: primary" in error
        for error in result.errors
    )


def test_workspace_source_policy_records_snapshot_for_clean_git_repo(
    tmp_path: Path,
) -> None:
    run_git_text(tmp_path, "init")
    run_git_text(tmp_path, "config", "user.name", "Crewplane Test")
    run_git_text(tmp_path, "config", "user.email", "crewplane-test@example.invalid")
    run_git_text(tmp_path, "config", "core.filemode", "false")
    run_git_text(tmp_path, "config", "core.protectHFS", "false")
    run_git_text(tmp_path, "config", "core.protectNTFS", "false")
    run_git_text(tmp_path, "config", "advice.statusHints", "false")
    (tmp_path / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(tmp_path, "add", "README.md")
    run_git_text(tmp_path, "commit", "-m", "initial")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=True,
        invoker_capabilities={
            "workspace": {
                "supported": True,
                "launch_mode": "runtime_command_runner",
                "honors_cwd": True,
                "controlled_child_environment": True,
            }
        },
    )

    assert result.errors == ()
    assert result.source_snapshot is not None
    assert result.source_snapshot.run_base_commit == run_git_text(
        tmp_path,
        "rev-parse",
        "HEAD^{commit}",
    )
    local_config_policy = result.source_snapshot.local_config_policy
    assert local_config_policy["rejected"] == ()
    assert "core.filemode" in local_config_policy["overridden"]
    assert "core.protecthfs" in local_config_policy["overridden"]
    assert "core.protectntfs" in local_config_policy["overridden"]
    assert "advice.statushints" in local_config_policy["ignored_neutral"]
    filesystem_capabilities = result.source_snapshot.filesystem_capabilities
    assert filesystem_capabilities["executable_bit"] is True
    assert filesystem_capabilities["symlink"] is True
    assert "case_sensitive" in filesystem_capabilities
    assert "unicode_normalization_sensitive" in filesystem_capabilities


def test_workspace_source_policy_overrides_line_ending_local_config(
    tmp_path: Path,
) -> None:
    run_git_text(tmp_path, "init")
    run_git_text(tmp_path, "config", "user.name", "Crewplane Test")
    run_git_text(tmp_path, "config", "user.email", "crewplane-test@example.invalid")
    run_git_text(tmp_path, "config", "core.autocrlf", "true")
    run_git_text(tmp_path, "config", "core.eol", "lf")
    (tmp_path / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(tmp_path, "add", "README.md")
    run_git_text(tmp_path, "commit", "-m", "initial")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.errors == ()
    assert result.source_snapshot is not None
    local_config_policy = result.source_snapshot.local_config_policy
    assert "core.autocrlf" in local_config_policy["overridden"]
    assert "core.eol" in local_config_policy["overridden"]


def test_workspace_source_policy_rejects_attribute_source_local_config(
    tmp_path: Path,
) -> None:
    run_git_text(tmp_path, "init")
    run_git_text(tmp_path, "config", "user.name", "Crewplane Test")
    run_git_text(tmp_path, "config", "user.email", "crewplane-test@example.invalid")
    run_git_text(tmp_path, "config", "core.attributesFile", "/tmp/attributes")
    (tmp_path / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(tmp_path, "add", "README.md")
    run_git_text(tmp_path, "commit", "-m", "initial")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert any(
        "local Git config contains unsupported keys" in error
        and "core.attributesfile" in error
        for error in result.errors
    )


@pytest.mark.parametrize(
    ("config_key", "config_value"),
    [
        ("remote.origin.promisor", "true"),
        ("remote.origin.partialclonefilter", "blob:none"),
    ],
)
def test_workspace_source_policy_rejects_partial_clone_remote_config(
    tmp_path: Path,
    config_key: str,
    config_value: str,
) -> None:
    _create_clean_repo(tmp_path)
    run_git_text(tmp_path, "config", config_key, config_value)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert any(
        "local Git config contains unsupported keys" in error
        and config_key in error
        and "settings.workspace.enabled: false" in error
        for error in result.errors
    )


@pytest.mark.parametrize("config_value", ["false", "0", "off", "no"])
def test_workspace_source_policy_allows_disabled_sparse_checkout_config(
    tmp_path: Path,
    config_value: str,
) -> None:
    _create_clean_repo(tmp_path)
    run_git_text(tmp_path, "config", "core.sparseCheckout", config_value)
    sparse_checkout_path = tmp_path / ".git" / "info" / "sparse-checkout"
    sparse_checkout_path.parent.mkdir(parents=True, exist_ok=True)
    sparse_checkout_path.write_text(
        "README.md\n",
        encoding="utf-8",
    )

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert not any("sparse checkout" in error for error in result.errors)


def test_workspace_source_policy_rejects_enabled_sparse_checkout_config(
    tmp_path: Path,
) -> None:
    _create_clean_repo(tmp_path)
    run_git_text(tmp_path, "config", "core.sparseCheckout", "true")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert any(
        "sparse checkout is unsupported" in error
        and "Use a full clone and full checkout" in error
        and "settings.workspace.enabled: false" in error
        for error in result.errors
    )


@pytest.mark.parametrize(
    ("label", "command", "config_key"),
    [
        ("split-index", ("update-index", "--split-index"), "core.splitIndex"),
        (
            "untracked-cache",
            ("update-index", "--untracked-cache"),
            "core.untrackedCache",
        ),
        ("fsmonitor", ("update-index", "--fsmonitor"), "core.fsmonitor"),
    ],
)
def test_workspace_source_policy_rejects_index_extension_state_without_config(
    tmp_path: Path,
    label: str,
    command: tuple[str, ...],
    config_key: str,
    isolated_git: IsolatedGit,
) -> None:
    _create_clean_repo(tmp_path)
    result = run_git(tmp_path, *command, check=False)
    if result.returncode != 0:
        isolated_git.unavailable(result.stderr)
    run_git(tmp_path, "config", "--unset", config_key, check=False)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert any(
        "Git index contains unsupported state" in error
        and label in error
        and "settings.workspace.enabled: false" in error
        for error in result.errors
    )


def _create_clean_repo(root: Path) -> None:
    run_git_text(root, "init")
    run_git_text(root, "config", "user.name", "Crewplane Test")
    run_git_text(root, "config", "user.email", "crewplane-test@example.invalid")
    (root / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(root, "add", "README.md")
    run_git_text(root, "commit", "-m", "initial")


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
