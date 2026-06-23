from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import crewplane.runtime.workspace.worktree as worktree_module
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.worktree import (
    WorktreeSourceRef,
    create_worktree_workspace,
    remove_worktree_workspace,
)
from crewplane.runtime.workspace.worktree.lineage import (
    worktree_protected_ref_scopes,
)
from crewplane.runtime.workspace.worktree.source_refs import (
    required_lineage_state,
)
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


def test_worktree_protected_ref_scope_covers_crewplane_namespace(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None

    scopes = worktree_protected_ref_scopes(
        plan,
        WorktreeSourceRef(
            source_kind="project",
            source_node_id=None,
            source_commit=source.run_base_commit,
            source_tree=source.source_tree,
        ),
        "implement",
        "implement-alpha-round1",
    )

    assert scopes == ("refs/crewplane",)


def test_worktree_reviewer_workspace_discards_drift_without_lineage(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output, role_label=ProviderRole.REVIEWER),
        workspace_invocation_context(role=ProviderRole.REVIEWER),
    )
    assert prepared.workspace_path is not None
    workspace_path = prepared.workspace_path
    source = plan.workspace_source
    assert source is not None
    assert workspace_path.parent == (
        cache_root
        / "review-workspaces"
        / source.repository_id
        / plan.run_key_name
        / "implement"
    )
    (prepared.cwd / "review-note.txt").write_text("discard me\n", encoding="utf-8")

    prepared.mark_succeeded()

    state = read_json_object(
        output.create_stage_dir("implement") / "workspace-state.json"
    )
    result = state["result"]
    assert isinstance(result, dict)
    assert result["lineage_produced"] is False
    assert result["changed_path_count"] == 1
    assert "result_commit" not in result
    assert "bundle" not in state
    diagnostics = state["diagnostics"]
    assert isinstance(diagnostics, list)
    assert diagnostics
    assert not workspace_path.exists()


def test_worktree_reviewer_workspace_reports_branch_attachment_drift(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output, role_label=ProviderRole.REVIEWER),
        workspace_invocation_context(role=ProviderRole.REVIEWER),
    )
    assert prepared.workspace_path is not None
    workspace_path = prepared.workspace_path
    run_git_text(prepared.cwd, "checkout", "-b", "review-branch")

    prepared.mark_succeeded()

    state = read_json_object(
        output.create_stage_dir("implement") / "workspace-state.json"
    )
    diagnostics = state["diagnostics"]
    assert isinstance(diagnostics, list)
    assert any("detached from branches" in str(item) for item in diagnostics)
    assert not workspace_path.exists()


def test_required_lineage_state_skips_disposable_reviewer_state(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = workspace_output_manager(tmp_path, repo)
    stage_dir = output.create_stage_dir("implement")
    reviewer_state = stage_dir / "workspace-state-a-reviewer.json"
    executor_state = stage_dir / "workspace-state-z-executor.json"
    reviewer_state.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "role": "reviewer",
                "task_id": "alpha",
                "round_num": 1,
                "audit_round_num": None,
                "workspace": {"lineage_producer": False},
                "result": {"lineage_produced": False, "changed_path_count": 0},
            }
        ),
        encoding="utf-8",
    )
    executor_state.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "role": "executor",
                "task_id": "alpha",
                "round_num": 1,
                "audit_round_num": None,
                "workspace": {"lineage_producer": True},
                "result": {
                    "result_commit": "a" * 40,
                    "result_tree": "b" * 40,
                },
            }
        ),
        encoding="utf-8",
    )

    assert required_lineage_state(output, "implement") == executor_state


def test_worktree_workspace_rejects_source_tree_mismatch_before_checkout(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    workspace_path = (
        cache_root
        / "workspaces"
        / source.repository_id
        / plan.run_key_name
        / "bad-source-tree"
    )
    wrong_tree = "f" * 40
    assert wrong_tree != source.source_tree

    with pytest.raises(RuntimeError, match="source tree mismatch"):
        create_worktree_workspace(
            plan,
            "bad-source-tree",
            source,
            WorktreeSourceRef(
                source_kind="project",
                source_node_id=None,
                source_commit=source.run_base_commit,
                source_tree=wrong_tree,
                candidate_sequence=0,
            ),
        )

    assert not workspace_path.exists()


def test_worktree_workspace_falls_back_to_explicit_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    real_git = worktree_module.git
    calls: list[tuple[str, ...]] = []

    class FallbackGitCommand:
        def __init__(self, command) -> None:
            self.command = command

        def run(self, *args: str):
            calls.append(args)
            if args[:5] == ("worktree", "add", "--detach", "--lock", "--reason"):
                raise subprocess.CalledProcessError(
                    129,
                    ("git", *args),
                    stderr=b"error: unknown option `lock'\nusage: git worktree add",
                )
            return self.command.run(*args)

        def text(self, *args: str) -> str:
            return self.command.text(*args)

        def zero_records(self, *args: str) -> tuple[str, ...]:
            return self.command.zero_records(*args)

    def fake_git(cwd: Path, index_path: Path | None = None) -> FallbackGitCommand:
        return FallbackGitCommand(real_git(cwd, index_path))

    monkeypatch.setattr(worktree_module, "git", fake_git)

    worktree = create_worktree_workspace(
        plan,
        "fallback-lock",
        source,
        WorktreeSourceRef(
            source_kind="project",
            source_node_id=None,
            source_commit=source.run_base_commit,
            source_tree=source.source_tree,
            candidate_sequence=0,
        ),
    )

    try:
        assert worktree.lock_mode == "lock_after_add"
        assert any(
            call[:3] == ("worktree", "add", "--detach") and call[3] != "--lock"
            for call in calls
        )
        assert any(
            call[:3] == ("worktree", "lock", "--reason")
            and call[3].startswith("crewplane ")
            for call in calls
        )
    finally:
        remove_worktree_workspace(source, worktree.workspace_path)
