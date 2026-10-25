from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.cli.run.workspace import repo_policy, source_policy
from crewplane.cli.run.workspace.source_types import WorkspacePolicyBuilder
from crewplane.core.config import Settings
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.workspace_source_policy import git_source_context

pytestmark = requires_workspace_support


@pytest.mark.parametrize(
    ("content", "rejected"),
    [
        (b"abc refs/heads/main\n", False),
        (b"abc refs/replace/original\n", True),
        (b"# abc refs/replace/comment\n", False),
        (b"^abc refs/replace/peeled\n", False),
        (b"abc refs/repl\xfface/original\n", True),
        (b"# abc refs/repl\xfface/comment\n", False),
        (b"^abc refs/repl\xfface/peeled\n", False),
    ],
)
def test_source_policy_preserves_packed_replacement_ref_filtering_and_diagnostic(
    tmp_path: Path, content: bytes, rejected: bool
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "packed-refs").write_bytes(content)
    builder = WorkspacePolicyBuilder()

    with (
        patch.object(repo_policy, "git_text", return_value="false"),
        patch.object(repo_policy, "git_config_value", return_value=None),
        patch.object(repo_policy, "git_config_bool", return_value=False),
    ):
        repo_policy.validate_unsupported_repo_state(
            tmp_path, git_source_context(tmp_path), builder
        )

    assert builder.errors == (
        [
            "Workspace source policy failed: Git replacement refs are unsupported. "
            "Workspace-enabled mode with worktree_contract: blob_exact fails "
            "closed before provider invocation. Use a standard local repository "
            "state before enabling workspace isolation, or set "
            "settings.workspace.enabled: false for this run."
        ]
        if rejected
        else []
    )


def test_source_policy_translates_replacement_ref_filesystem_failure(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "packed-refs").write_bytes(b"abc refs/heads/main\n")
    builder = WorkspacePolicyBuilder()

    with (
        patch.object(source_policy, "validate_cache_root", return_value=None),
        patch.object(repo_policy, "git_text", return_value="false"),
        patch.object(repo_policy, "git_config_value", return_value=None),
        patch.object(repo_policy, "git_config_bool", return_value=False),
        patch.object(
            Path, "read_text", side_effect=PermissionError("packed refs unreadable")
        ),
    ):
        local_config, filesystem = source_policy.collect_git_source_checks(
            Settings(workspace={"enabled": True}),
            tmp_path,
            tmp_path / ".crewplane",
            git_source_context(tmp_path),
            True,
            ("primary",),
            False,
            builder,
        )

    assert local_config == {}
    assert filesystem == {}
    assert builder.errors == [
        "Workspace source policy failed: Git source inspection failed "
        "(packed refs unreadable)."
    ]
