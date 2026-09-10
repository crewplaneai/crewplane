from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.cli.run.workspace import git_source
from crewplane.cli.run.workspace import source_policy as policy
from crewplane.cli.run.workspace.git_source import discover_git_context
from crewplane.cli.run.workspace.source_types import WorkspacePolicyBuilder
from crewplane.core.config import Settings
from tests.helpers import isolated_git as _isolated_git_support
from tests.helpers.isolated_git import (
    run_git_text,
)
from tests.helpers.workspace_source_policy import (
    git_source_context,
    workspace_source_config,
    workspace_source_workflow,
)
from tests.unit.cli.workspace_source_policy_git_support import (
    create_clean_source_repo,
)

isolated_git = _isolated_git_support.isolated_git


pytestmark = pytest.mark.usefixtures("isolated_git")


def test_workspace_source_policy_allows_regular_policy_files_and_source_symlinks(
    tmp_path: Path,
) -> None:
    create_clean_source_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("*.md diff=markdown\n", encoding="utf-8")
    (tmp_path / "readme-link").symlink_to("README.md")
    run_git_text(tmp_path, "add", ".")
    run_git_text(tmp_path, "commit", "-m", "record policies and source symlink")

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.errors == ()
    assert result.source_snapshot is not None


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


def test_discover_git_context_pairs_tree_with_captured_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_clean_source_repo(tmp_path)
    captured_commit = run_git_text(tmp_path, "rev-parse", "HEAD^{commit}")
    captured_tree = run_git_text(
        tmp_path,
        "rev-parse",
        f"{captured_commit}^{{tree}}",
    )
    original_git_text = git_source.git_text
    head_advanced = False

    def advance_head_after_commit_read(project_root: Path, *args: str) -> str:
        nonlocal head_advanced
        result = original_git_text(project_root, *args)
        if args == ("rev-parse", "HEAD^{commit}") and not head_advanced:
            (tmp_path / "next.txt").write_text("next\n", encoding="utf-8")
            run_git_text(tmp_path, "add", "next.txt")
            run_git_text(tmp_path, "commit", "-m", "advance head")
            head_advanced = True
        return result

    monkeypatch.setattr(git_source, "git_text", advance_head_after_commit_read)
    builder = WorkspacePolicyBuilder()

    context = discover_git_context(tmp_path, builder)

    assert builder.errors == []
    assert context is not None
    assert context.run_base_commit == captured_commit
    assert context.source_tree == captured_tree
    assert run_git_text(tmp_path, "rev-parse", "HEAD^{commit}") != captured_commit


def test_workspace_source_policy_rejects_head_change_during_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_clean_source_repo(tmp_path)
    original_warn_storage_pressure = policy.warn_storage_pressure

    def warn_then_advance_head(
        settings: Settings,
        git_context: git_source.GitSourceContext,
        estimate_full_repository: bool,
        builder: WorkspacePolicyBuilder,
    ) -> None:
        original_warn_storage_pressure(
            settings,
            git_context,
            estimate_full_repository,
            builder,
        )
        (tmp_path / "next.txt").write_text("next\n", encoding="utf-8")
        run_git_text(tmp_path, "add", "next.txt")
        run_git_text(tmp_path, "commit", "-m", "advance head")

    monkeypatch.setattr(policy, "warn_storage_pressure", warn_then_advance_head)

    result = policy.collect_workspace_source_policy(
        config=workspace_source_config(),
        workflow=workspace_source_workflow(),
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        real_execution=False,
    )

    assert result.source_snapshot is None
    assert any(
        "Git HEAD changed during workspace source validation" in error
        for error in result.errors
    )


def test_discover_git_context_preserves_trailing_space_in_repository_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project "
    project_root.mkdir()
    create_clean_source_repo(project_root)
    builder = WorkspacePolicyBuilder()

    context = discover_git_context(project_root, builder)

    assert builder.errors == []
    assert context is not None
    assert context.git_top_level == project_root.resolve()


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


def test_workspace_source_policy_clean_start_names_logical_worktree(
    tmp_path: Path,
) -> None:
    create_clean_source_repo(tmp_path)
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
