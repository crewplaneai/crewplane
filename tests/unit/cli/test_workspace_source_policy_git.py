from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator_cli.cli.run import workspace_source_policy as policy
from orchestrator_cli.cli.run.git_source_probe import (
    GIT_MIN_VERSION,
    discover_git_context,
    parse_git_version,
)
from orchestrator_cli.cli.run.workspace_source_types import WorkspacePolicyBuilder
from orchestrator_cli.core.config import Settings
from tests.helpers.workspace_source_policy import (
    git_source_context,
    run_git_text,
    workspace_source_config,
    workspace_source_workflow,
)


def test_workspace_source_policy_ignores_untracked_attributes_with_tracked_only(
    tmp_path: Path,
) -> None:
    _require_workspace_git()
    run_git_text(tmp_path, "init")
    run_git_text(tmp_path, "config", "user.name", "Orchestrator Test")
    run_git_text(tmp_path, "config", "user.email", "orchestrator-test@example.invalid")
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
        orchestrator_dir=tmp_path / ".orchestrator",
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
    _require_workspace_git()

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        orchestrator_dir=tmp_path / ".orchestrator",
        real_execution=False,
    )

    assert result.source_snapshot is None
    assert len(result.errors) == 1
    assert "requires a Git repository with a valid HEAD commit" in result.errors[0]
    assert "settings.workspace.enabled: false" in result.errors[0]


def test_discover_git_context_resolves_common_dir_from_project_root(
    tmp_path: Path,
) -> None:
    _require_workspace_git()
    project_root = tmp_path / "project"
    nested_root = project_root / "app"
    nested_root.mkdir(parents=True)
    run_git_text(project_root, "init")
    run_git_text(project_root, "config", "user.name", "Orchestrator Test")
    run_git_text(
        project_root, "config", "user.email", "orchestrator-test@example.invalid"
    )
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
        tmp_path / ".orchestrator",
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
    _require_workspace_git()
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
        orchestrator_dir=tmp_path / ".orchestrator",
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
    _require_workspace_git()
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
        orchestrator_dir=tmp_path / ".orchestrator",
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
    _require_workspace_git()
    _create_clean_repo(tmp_path)
    (tmp_path / ".gitattributes").write_text("*.md text=auto\n", encoding="utf-8")
    run_git_text(tmp_path, "add", ".gitattributes")
    run_git_text(tmp_path, "commit", "-m", "text attributes")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        orchestrator_dir=tmp_path / ".orchestrator",
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
    _require_workspace_git()
    _create_clean_repo(tmp_path)
    (tmp_path / "README.md").write_text("dirty\n", encoding="utf-8")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        orchestrator_dir=tmp_path / ".orchestrator",
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
    _require_workspace_git()
    run_git_text(tmp_path, "init")
    run_git_text(tmp_path, "config", "user.name", "Orchestrator Test")
    run_git_text(tmp_path, "config", "user.email", "orchestrator-test@example.invalid")
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
        orchestrator_dir=tmp_path / ".orchestrator",
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
    _require_workspace_git()
    run_git_text(tmp_path, "init")
    run_git_text(tmp_path, "config", "user.name", "Orchestrator Test")
    run_git_text(tmp_path, "config", "user.email", "orchestrator-test@example.invalid")
    run_git_text(tmp_path, "config", "core.autocrlf", "true")
    run_git_text(tmp_path, "config", "core.eol", "lf")
    (tmp_path / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(tmp_path, "add", "README.md")
    run_git_text(tmp_path, "commit", "-m", "initial")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        orchestrator_dir=tmp_path / ".orchestrator",
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
    _require_workspace_git()
    run_git_text(tmp_path, "init")
    run_git_text(tmp_path, "config", "user.name", "Orchestrator Test")
    run_git_text(tmp_path, "config", "user.email", "orchestrator-test@example.invalid")
    run_git_text(tmp_path, "config", "core.attributesFile", "/tmp/attributes")
    (tmp_path / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(tmp_path, "add", "README.md")
    run_git_text(tmp_path, "commit", "-m", "initial")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        orchestrator_dir=tmp_path / ".orchestrator",
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
    _require_workspace_git()
    _create_clean_repo(tmp_path)
    run_git_text(tmp_path, "config", config_key, config_value)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        orchestrator_dir=tmp_path / ".orchestrator",
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
    _require_workspace_git()
    _create_clean_repo(tmp_path)
    run_git_text(tmp_path, "config", "core.sparseCheckout", config_value)
    (tmp_path / ".git" / "info" / "sparse-checkout").write_text(
        "README.md\n",
        encoding="utf-8",
    )

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        orchestrator_dir=tmp_path / ".orchestrator",
        real_execution=False,
    )

    assert not any("sparse checkout" in error for error in result.errors)


def test_workspace_source_policy_rejects_enabled_sparse_checkout_config(
    tmp_path: Path,
) -> None:
    _require_workspace_git()
    _create_clean_repo(tmp_path)
    run_git_text(tmp_path, "config", "core.sparseCheckout", "true")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        orchestrator_dir=tmp_path / ".orchestrator",
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
) -> None:
    _require_workspace_git()
    _create_clean_repo(tmp_path)
    result = subprocess.run(
        ["git", "-C", tmp_path.as_posix(), *command],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip(result.stderr.decode("utf-8", errors="replace"))
    subprocess.run(
        ["git", "-C", tmp_path.as_posix(), "config", "--unset", config_key],
        check=False,
        capture_output=True,
    )

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        orchestrator_dir=tmp_path / ".orchestrator",
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
    run_git_text(root, "config", "user.name", "Orchestrator Test")
    run_git_text(root, "config", "user.email", "orchestrator-test@example.invalid")
    (root / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(root, "add", "README.md")
    run_git_text(root, "commit", "-m", "initial")


def _require_workspace_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    git_version = subprocess.run(
        ["git", "--version"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    parsed_version = parse_git_version(git_version)
    if parsed_version is None or parsed_version < GIT_MIN_VERSION:
        pytest.skip("Git 2.34.1+ is required for the workspace source policy")
