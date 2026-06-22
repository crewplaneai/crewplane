from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from orchestrator_cli.cli.run import (
    workspace_git_attributes,
    workspace_repo_policy,
)
from orchestrator_cli.cli.run import workspace_source_policy as policy
from orchestrator_cli.core.config import Settings
from tests.helpers.workspace_source_policy import git_source_context


def test_workspace_source_policy_rejects_unsupported_git_state_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "replace").mkdir(parents=True)
    (git_dir / "refs" / "replace" / ("a" * 40)).write_text(
        "b" * 40,
        encoding="utf-8",
    )
    (git_dir / "objects" / "pack").mkdir(parents=True)
    (git_dir / "objects" / "pack" / "pack-test.promisor").write_text(
        "",
        encoding="utf-8",
    )
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "sparse-checkout").write_text(
        "*.py\n",
        encoding="utf-8",
    )
    (git_dir / "MERGE_HEAD").write_text("c" * 40, encoding="utf-8")

    def clean_git_text(*args: object) -> str:
        del args
        return "false"

    def missing_git_config(*args: object) -> None:
        del args
        return None

    def enabled_git_config_bool(*args: object) -> bool:
        del args
        return True

    monkeypatch.setattr(workspace_repo_policy, "git_text", clean_git_text)
    monkeypatch.setattr(workspace_repo_policy, "git_config_value", missing_git_config)
    monkeypatch.setattr(
        workspace_repo_policy,
        "git_config_bool",
        enabled_git_config_bool,
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_unsupported_repo_state(
        tmp_path,
        git_source_context(tmp_path),
        builder,
    )

    assert any("replacement refs" in error for error in builder.errors)
    assert any("promisor object packs" in error for error in builder.errors)
    assert any("sparse checkout state" in error for error in builder.errors)
    assert any("in-progress merge state" in error for error in builder.errors)
    assert all(has_disable_workspace_remediation(error) for error in builder.errors)


def test_workspace_source_policy_rejects_shallow_partial_and_sparse_with_remediation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def shallow_git_text(*args: object) -> str:
        del args
        return "true"

    def partial_clone_git_config(*args: object) -> str:
        del args
        return "origin"

    def enabled_git_config_bool(*args: object) -> bool:
        del args
        return True

    monkeypatch.setattr(workspace_repo_policy, "git_text", shallow_git_text)
    monkeypatch.setattr(
        workspace_repo_policy,
        "git_config_value",
        partial_clone_git_config,
    )
    monkeypatch.setattr(
        workspace_repo_policy,
        "git_config_bool",
        enabled_git_config_bool,
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_unsupported_repo_state(
        tmp_path,
        git_source_context(tmp_path),
        builder,
    )

    assert any(
        "shallow repositories are unsupported" in error for error in builder.errors
    )
    assert any(
        "partial clone repositories are unsupported" in error
        for error in builder.errors
    )
    assert any("sparse checkout is unsupported" in error for error in builder.errors)
    assert all("full clone and full checkout" in error for error in builder.errors[:3])
    assert all(has_disable_workspace_remediation(error) for error in builder.errors)


def test_workspace_source_policy_allows_disabled_sparse_checkout_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def clean_git_text(*args: object) -> str:
        del args
        return "false"

    def missing_git_config(*args: object) -> None:
        del args
        return None

    def disabled_git_config_bool(*args: object) -> bool:
        del args
        return False

    monkeypatch.setattr(workspace_repo_policy, "git_text", clean_git_text)
    monkeypatch.setattr(workspace_repo_policy, "git_config_value", missing_git_config)
    monkeypatch.setattr(
        workspace_repo_policy,
        "git_config_bool",
        disabled_git_config_bool,
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_unsupported_repo_state(
        tmp_path,
        git_source_context(tmp_path),
        builder,
    )

    assert not any("sparse checkout" in error for error in builder.errors)


def test_workspace_source_policy_inspects_local_config_without_includes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_args: tuple[str, ...] = ()

    def fake_git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        nonlocal captured_args
        assert project_root == tmp_path
        captured_args = args
        return ("local", "file:.git/config", "core.attributesFile")

    monkeypatch.setattr(
        workspace_repo_policy,
        "git_zero_records",
        fake_git_zero_records,
    )
    builder = policy.WorkspacePolicyBuilder()

    summary = workspace_repo_policy.validate_local_git_config(tmp_path, builder)

    assert "--no-includes" in captured_args
    assert "--show-origin" in captured_args
    assert "--show-scope" in captured_args
    assert summary["rejected"] == ("core.attributesfile",)
    assert any(
        "core.attributesfile" in error and has_disable_workspace_remediation(error)
        for error in builder.errors
    )


def test_workspace_source_policy_rejects_linked_worktree_local_policy_files(
    tmp_path: Path,
) -> None:
    common_git_dir = tmp_path / ".git"
    active_git_dir = common_git_dir / "worktrees" / "linked"
    (common_git_dir / "info").mkdir(parents=True)
    (active_git_dir / "info").mkdir(parents=True)
    (active_git_dir / "info" / "attributes").write_text(
        "README.md filter=lfs\n",
        encoding="utf-8",
    )
    (active_git_dir / "info" / "exclude").write_text(
        "local-cache/\n",
        encoding="utf-8",
    )
    git_context = replace(
        git_source_context(tmp_path),
        active_git_dir=active_git_dir,
        common_git_dir=common_git_dir,
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_local_policy_files(git_context, builder)

    assert any(
        "Git info/attributes" in error
        and (active_git_dir / "info" / "attributes").as_posix() in error
        for error in builder.errors
    )
    assert any(
        "Git info/exclude" in error
        and (active_git_dir / "info" / "exclude").as_posix() in error
        for error in builder.errors
    )


def test_workspace_source_policy_dedupes_shared_git_admin_policy_files(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "info").mkdir(parents=True)
    (git_dir / "info" / "attributes").write_text(
        "README.md filter=lfs\n",
        encoding="utf-8",
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_local_policy_files(
        git_source_context(tmp_path),
        builder,
    )

    assert len(builder.errors) == 1
    assert builder.errors[0].count((git_dir / "info" / "attributes").as_posix()) == 1


def test_workspace_source_policy_rejects_byte_transforming_attributes_and_path_collisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        del project_root
        if "check-attr" in args:
            return ("README.md", "text", "auto")
        return (
            "100644 blob a\t.gitattributes",
            "100644 blob b\tReadme.md",
            "100644 blob c\tREADME.md",
            "100644 blob d\tCafe\u0301.md",
            "100644 blob e\tCafé.md",
        )

    def fake_git_zero_records_with_env(
        project_root: Path,
        args: tuple[str, ...],
        env_overrides: dict[str, str],
    ) -> tuple[str, ...]:
        del project_root, env_overrides
        if "check-attr" in args:
            return ("README.md", "text", "auto")
        return ()

    def fake_run_git_with_env(*args: object) -> None:
        del args

    def fake_path_exists(*args: object) -> bool:
        del args
        return False

    monkeypatch.setattr(
        workspace_repo_policy,
        "git_zero_records",
        fake_git_zero_records,
    )
    monkeypatch.setattr(
        workspace_git_attributes,
        "git_zero_records_with_env",
        fake_git_zero_records_with_env,
    )
    monkeypatch.setattr(
        workspace_git_attributes,
        "run_git_with_env",
        fake_run_git_with_env,
    )
    monkeypatch.setattr(
        workspace_repo_policy,
        "git_path_exists",
        fake_path_exists,
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_source_tree(git_source_context(tmp_path), builder)

    assert any("byte-transforming Git attributes" in error for error in builder.errors)
    assert any("case or Unicode normalization" in error for error in builder.errors)


def test_workspace_source_policy_strict_clean_start_rejects_untracked_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        assert project_root == tmp_path
        assert "status" in args
        return ("? src/new.py",)

    monkeypatch.setattr(
        workspace_repo_policy,
        "git_zero_records",
        git_zero_records,
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_clean_start(
        tmp_path,
        Settings(workspace={"enabled": True}),
        builder,
        ("primary",),
    )

    assert any(
        "strict clean_start rejects untracked source files" in error
        and "src/new.py" in error
        and "Required by logical worktrees: primary" in error
        for error in builder.errors
    )


def test_clean_start_reserved_paths_are_project_root_relative(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    git_context = replace(
        git_source_context(tmp_path),
        project_root_relative_path="app",
    )

    def git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        assert project_root == tmp_path / "app"
        assert "status" in args
        return (
            "? app/.orchestrator/execution-stages/log.txt",
            "? app/src/new.py",
            "? root.txt",
        )

    monkeypatch.setattr(
        workspace_repo_policy,
        "git_zero_records",
        git_zero_records,
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_clean_start(
        tmp_path / "app",
        Settings(workspace={"enabled": True}),
        builder,
        ("primary",),
        git_context,
    )

    assert len(builder.errors) == 1
    assert "app/src/new.py" in builder.errors[0]
    assert "root.txt" not in builder.errors[0]
    assert ".orchestrator/execution-stages" not in builder.errors[0]


def test_workspace_source_policy_rejects_gitlinks_and_gitmodules(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        assert project_root == tmp_path
        assert "ls-tree" in args
        return (
            "160000 commit abcdef\tvendor/lib",
            "100644 blob abcdef\tREADME.md",
        )

    def git_path_exists(project_root: Path, spec: str) -> bool:
        assert project_root == tmp_path
        return spec.endswith(":.gitmodules")

    def validate_attributes(*args: object) -> None:
        del args

    monkeypatch.setattr(
        workspace_repo_policy,
        "git_zero_records",
        git_zero_records,
    )
    monkeypatch.setattr(
        workspace_repo_policy,
        "git_path_exists",
        git_path_exists,
    )
    monkeypatch.setattr(
        workspace_repo_policy,
        "validate_byte_transforming_attributes",
        validate_attributes,
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_source_tree(git_source_context(tmp_path), builder)

    assert any(
        "submodules/gitlinks are unsupported" in error
        and "vendor/lib" in error
        and has_disable_workspace_remediation(error)
        for error in builder.errors
    )
    assert any(
        "repositories with .gitmodules are unsupported" in error
        and has_disable_workspace_remediation(error)
        for error in builder.errors
    )


def test_source_tree_reserved_paths_are_project_root_relative(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    git_context = replace(
        git_source_context(tmp_path),
        project_root_relative_path="app",
    )

    def git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        assert project_root == tmp_path
        assert "ls-tree" in args
        assert "--full-tree" in args
        assert "--full-name" in args
        return (
            "100644 blob a\t.orchestrator/execution-stages/root.txt",
            "100644 blob b\tapp/.orchestrator/execution-stages/stage.txt",
            "100644 blob c\tapp/src/app.py",
        )

    def git_path_exists(project_root: Path, spec: str) -> bool:
        del project_root, spec
        return False

    def validate_attributes(*args: object) -> None:
        del args

    monkeypatch.setattr(
        workspace_repo_policy,
        "git_zero_records",
        git_zero_records,
    )
    monkeypatch.setattr(
        workspace_repo_policy,
        "git_path_exists",
        git_path_exists,
    )
    monkeypatch.setattr(
        workspace_repo_policy,
        "validate_byte_transforming_attributes",
        validate_attributes,
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_source_tree(git_context, builder)

    assert len(builder.errors) == 1
    assert "app/.orchestrator/execution-stages/stage.txt" in builder.errors[0]
    assert ".orchestrator/execution-stages/root.txt" not in builder.errors[0]


def test_workspace_source_policy_allows_harmless_tracked_attributes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        del project_root
        if "check-attr" in args:
            return ("README.md", "diff", "markdown")
        return (
            "100644 blob a\t.gitattributes",
            "100644 blob b\tREADME.md",
        )

    def fake_git_zero_records_with_env(
        project_root: Path,
        args: tuple[str, ...],
        env_overrides: dict[str, str],
    ) -> tuple[str, ...]:
        del project_root, env_overrides
        if "check-attr" in args:
            return ("README.md", "diff", "markdown")
        return ()

    def fake_run_git_with_env(*args: object) -> None:
        del args

    def fake_path_exists(*args: object) -> bool:
        del args
        return False

    monkeypatch.setattr(
        workspace_repo_policy,
        "git_zero_records",
        fake_git_zero_records,
    )
    monkeypatch.setattr(
        workspace_git_attributes,
        "git_zero_records_with_env",
        fake_git_zero_records_with_env,
    )
    monkeypatch.setattr(
        workspace_git_attributes,
        "run_git_with_env",
        fake_run_git_with_env,
    )
    monkeypatch.setattr(
        workspace_repo_policy,
        "git_path_exists",
        fake_path_exists,
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_repo_policy.validate_source_tree(git_source_context(tmp_path), builder)

    assert builder.errors == []


def has_disable_workspace_remediation(error: str) -> bool:
    return "settings.workspace.enabled: false" in error
