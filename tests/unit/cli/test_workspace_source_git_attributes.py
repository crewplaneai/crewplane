from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.cli.run.workspace import source_policy as policy
from tests.helpers import isolated_git as _isolated_git_support
from tests.helpers.isolated_git import (
    IsolatedGit,
    run_git,
    run_git_text,
)
from tests.helpers.workspace_source_policy import (
    workspace_source_config,
    workspace_source_workflow,
)
from tests.unit.cli.workspace_source_policy_git_support import (
    create_clean_source_repo,
)

isolated_git = _isolated_git_support.isolated_git


pytestmark = pytest.mark.usefixtures("isolated_git")


def test_workspace_source_policy_rejects_lfs_attributes_with_remediation(
    tmp_path: Path,
) -> None:
    create_clean_source_repo(tmp_path)
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
    create_clean_source_repo(tmp_path)
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
    create_clean_source_repo(tmp_path)
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
    create_clean_source_repo(tmp_path)
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
    create_clean_source_repo(tmp_path)
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
    create_clean_source_repo(tmp_path)
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
    create_clean_source_repo(tmp_path)
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
