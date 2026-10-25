from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.runtime.workspace.worktree import (
    create_worktree_workspace,
    policy,
    remove_worktree_workspace,
    reuse,
)
from crewplane.runtime.workspace.worktree.reset import reset_reusable_worktree_checkout
from crewplane.runtime.workspace.worktree.types import (
    WorktreeProvisioningClaim,
    WorktreeSourceRef,
)
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.workspace_service import (
    create_git_repo,
    workspace_plan,
)

pytestmark = requires_workspace_support


@pytest.mark.parametrize(
    ("content", "rejected"),
    [
        (b"abc refs/heads/main\n", False),
        (b"abc refs/replace/original\n", True),
        (b"# abc refs/replace/comment\n", True),
        (b"^abc refs/replace/peeled\n", True),
        (b"abc refs/repl\xfface/original\n", True),
        (b"# abc refs/repl\xfface/comment\n", True),
        (b"^abc refs/repl\xfface/peeled\n", True),
    ],
)
def test_common_policy_inspects_all_packed_records_and_preserves_diagnostic(
    tmp_path: Path, content: bytes, rejected: bool
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "packed-refs").write_bytes(content)

    with patch.object(policy, "common_git_config_rejected_keys", return_value=()):
        if rejected:
            with pytest.raises(RuntimeError) as caught:
                policy.reject_common_git_policy_drift(tmp_path, git_dir)
            assert str(caught.value) == (
                "Workspace common Git replacement refs must not exist."
            )
        else:
            policy.reject_common_git_policy_drift(tmp_path, git_dir)


def test_common_policy_propagates_replacement_ref_filesystem_failure(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "packed-refs").write_bytes(b"abc refs/heads/main\n")
    failure = PermissionError("packed refs unreadable")

    with (
        patch.object(policy, "common_git_config_rejected_keys", return_value=()),
        patch.object(Path, "read_text", side_effect=failure),
        pytest.raises(PermissionError) as caught,
    ):
        policy.reject_common_git_policy_drift(tmp_path, git_dir)

    assert caught.value is failure


def test_fresh_worktree_creation_rejects_replacement_refs_before_ready(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", False, kind="worktree")
    source = plan.workspace_source
    assert source is not None
    source_ref = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
    )
    ref = Path(source.common_git_dir) / "refs" / "replace" / source.run_base_commit
    claims: list[WorktreeProvisioningClaim] = []

    def inject_replacement_ref(
        claim: WorktreeProvisioningClaim, generation: int
    ) -> None:
        del generation
        claims.append(claim)
        ref.parent.mkdir(parents=True)
        ref.write_text(f"{source.run_base_commit}\n", encoding="utf-8")

    try:
        with pytest.raises(RuntimeError) as caught:
            create_worktree_workspace(
                plan,
                "fresh",
                source,
                source_ref,
                record_fresh_claim=inject_replacement_ref,
            )
        assert len(claims) == 1
        assert str(caught.value) == (
            "Workspace common Git replacement refs must not exist."
        )
    finally:
        ref.unlink(missing_ok=True)
        for claim in claims:
            remove_worktree_workspace(source, claim.workspace_path, claim.git_dir)


def test_reused_worktree_rejects_replacement_refs_introduced_after_reset(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", False, kind="worktree")
    source = plan.workspace_source
    assert source is not None
    source_ref = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
    )
    ref = Path(source.common_git_dir) / "refs" / "replace" / source.run_base_commit
    workspace = create_worktree_workspace(plan, "reused", source, source_ref)

    def reset_then_inject_replacement_ref(
        checkout_root: Path,
        source_commit: str,
        repo_root: Path,
        common_git_dir: Path,
        expected_git_dir: Path,
    ) -> None:
        reset_reusable_worktree_checkout(
            checkout_root, source_commit, repo_root, common_git_dir, expected_git_dir
        )
        ref.parent.mkdir(parents=True)
        ref.write_text(f"{source.run_base_commit}\n", encoding="utf-8")

    try:
        with (
            patch.object(
                reuse,
                "reset_reusable_worktree_checkout",
                new=reset_then_inject_replacement_ref,
            ),
            pytest.raises(RuntimeError) as caught,
        ):
            reuse.reuse_worktree_workspace(
                workspace.workspace_path, source, source_ref, workspace.git_dir
            )
        assert ref.is_file()
        assert workspace.checkout_root.is_dir()
        assert str(caught.value) == (
            "Workspace common Git replacement refs must not exist."
        )
    finally:
        ref.unlink(missing_ok=True)
        remove_worktree_workspace(source, workspace.workspace_path, workspace.git_dir)
