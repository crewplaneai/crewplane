from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import crewplane.runtime.execution.workspace_files.resolution as workspace_file_resolution
from crewplane.core.preflight.models import (
    Fragment,
    RenderPlan,
    RenderStream,
    WorkspaceFileLocator,
    WorkspaceSourceSnapshot,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.fragment_assembler import assemble_prompt
from tests.helpers.workspace_records import (
    WORKTREE_CONTRACT,
    workspace_selection_record,
)
from tests.integration.runtime.execution.fragment_assembler_support import (
    FragmentArtifactStore,
    make_fragment_plan,
    run_fragment_git,
    write_lineage_state_with_bundle,
)


def test_assemble_prompt_rejects_runtime_dynamic_workspace_file_locator(
    tmp_path: Path,
) -> None:
    context_root = tmp_path / "execution-stages" / "demo-run"
    plan = make_fragment_plan(context_root).model_copy(
        update={
            "workspace_file_locators": [
                WorkspaceFileLocator(
                    locator_id="workspace-file-dynamic",
                    occurrence_id="build:executor:0:file:future.md",
                    node_id="build",
                    target="executor_prompt",
                    source_class="runtime_dynamic",
                    raw_token="{{file:future.md}}",
                    raw_path="future.md",
                    source_root=tmp_path.as_posix(),
                    source_root_relative_to_project=".",
                    project_root_relative_to_git_top=".",
                    git_top_relative_path="future.md",
                    workspace_relative_path="future.md",
                )
            ],
            "render_plans": [
                RenderPlan(
                    render_plan_id="build",
                    node_id="build",
                    streams=[
                        RenderStream(
                            target_role=ProviderRole.EXECUTOR,
                            fragments=[
                                Fragment(
                                    fragment_index=0,
                                    kind="workspace_file_locator",
                                    source_role=PromptSegmentRole.SHARED,
                                    locator={
                                        "locator_id": "workspace-file-dynamic",
                                        "source_class": "runtime_dynamic",
                                        "workspace_relative_path": "future.md",
                                    },
                                )
                            ],
                        )
                    ],
                )
            ],
        }
    )
    store = FragmentArtifactStore(tmp_path)
    secrets = SecretContext()

    with pytest.raises(RuntimeError, match="Runtime-dynamic workspace file locator"):
        assemble_prompt(plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets)


def test_assemble_prompt_reads_runtime_dynamic_workspace_file_locator(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_fragment_git(repo, "init")
    run_fragment_git(repo, "config", "user.name", "Crewplane Test")
    run_fragment_git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    run_fragment_git(repo, "add", "README.md")
    run_fragment_git(repo, "commit", "-m", "base")
    source_commit = run_fragment_git(repo, "rev-parse", "HEAD^{commit}")
    source_tree = run_fragment_git(repo, "rev-parse", "HEAD^{tree}")
    (repo / "future.md").write_text("dynamic\n", encoding="utf-8")
    run_fragment_git(repo, "add", "future.md")
    run_fragment_git(repo, "commit", "-m", "candidate")
    result_commit = run_fragment_git(repo, "rev-parse", "HEAD^{commit}")
    result_tree = run_fragment_git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = FragmentArtifactStore(tmp_path)
    upstream_stage = store.stages_dir / "input-stage"
    write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=upstream_stage,
        source_commit=source_commit,
        source_tree=source_tree,
        result_commit=result_commit,
        result_tree=result_tree,
        bundle_name="input.bundle",
        extra_payload={"node_id": "input"},
    )
    locator = WorkspaceFileLocator(
        locator_id="workspace-file-dynamic",
        occurrence_id="build:executor:0:file:future.md",
        node_id="build",
        target="executor_prompt",
        source_class="runtime_dynamic",
        raw_token="{{file:future.md}}",
        raw_path="future.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="future.md",
        workspace_relative_path="future.md",
    )
    plan = make_fragment_plan(context_root).model_copy(
        update={
            "workspace_source": WorkspaceSourceSnapshot(
                worktree_contract=WORKTREE_CONTRACT,
                run_base_commit=source_commit,
                source_tree=source_tree,
                object_format="sha1",
                repository_id="repo",
                git_version=run_fragment_git(repo, "--version"),
                git_top_level=repo.as_posix(),
                project_root_relative_path=".",
                active_git_dir=(repo / ".git").as_posix(),
                common_git_dir=(repo / ".git").as_posix(),
                clean_start="strict",
            ),
            "workspace_file_locators": [locator],
            "nodes": [
                make_fragment_plan(context_root).nodes[0],
                make_fragment_plan(context_root)
                .nodes[1]
                .model_copy(
                    update={
                        "workspace_policy": workspace_selection_record(
                            enabled=True,
                            kind="worktree",
                            source_kind="node",
                            source_node_id="input",
                            clean_start="strict",
                            materialization="worktree_checkout",
                        )
                    }
                ),
            ],
            "render_plans": [
                RenderPlan(
                    render_plan_id="build",
                    node_id="build",
                    streams=[
                        RenderStream(
                            target_role=ProviderRole.EXECUTOR,
                            fragments=[
                                Fragment(
                                    fragment_index=0,
                                    kind="workspace_file_locator",
                                    source_role=PromptSegmentRole.SHARED,
                                    locator={
                                        "locator_id": "workspace-file-dynamic",
                                        "source_class": "runtime_dynamic",
                                        "workspace_relative_path": "future.md",
                                    },
                                )
                            ],
                        )
                    ],
                )
            ],
        }
    )

    prompt = assemble_prompt(
        plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
    )

    assert prompt == "dynamic\n"

    missing_file_commit = run_fragment_git(
        repo,
        "commit-tree",
        source_tree,
        "-p",
        source_commit,
        "-m",
        "candidate without file",
    )
    missing_file_tree = source_tree
    write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=upstream_stage,
        source_commit=source_commit,
        source_tree=source_tree,
        result_commit=missing_file_commit,
        result_tree=missing_file_tree,
        bundle_name="input-missing-file.bundle",
        extra_payload={"node_id": "input"},
    )

    with pytest.raises(
        NodeExecutionError,
        match="Runtime-dynamic workspace file locator does not resolve exactly",
    ):
        assemble_prompt(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )


def test_assemble_prompt_imports_bundle_for_runtime_dynamic_workspace_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_fragment_git(repo, "init")
    run_fragment_git(repo, "config", "user.name", "Crewplane Test")
    run_fragment_git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    run_fragment_git(repo, "add", "README.md")
    run_fragment_git(repo, "commit", "-m", "base")
    parent_commit = run_fragment_git(repo, "rev-parse", "HEAD^{commit}")
    parent_tree = run_fragment_git(repo, "rev-parse", "HEAD^{tree}")
    (repo / "future.md").write_text("bundled dynamic\n", encoding="utf-8")
    run_fragment_git(repo, "add", "future.md")
    result_tree = run_fragment_git(repo, "write-tree")
    result_commit = run_fragment_git(
        repo,
        "commit-tree",
        result_tree,
        "-p",
        parent_commit,
        "-m",
        "candidate",
    )
    run_fragment_git(repo, "reset", "--hard", parent_commit)

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = FragmentArtifactStore(tmp_path)
    upstream_stage = store.stages_dir / "input-stage"
    result_ref = write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=upstream_stage,
        source_commit=parent_commit,
        source_tree=parent_tree,
        result_commit=result_commit,
        result_tree=result_tree,
        bundle_name="input.bundle",
        extra_payload={"node_id": "input"},
    )
    run_fragment_git(repo, "update-ref", "-d", result_ref)
    run_fragment_git(repo, "reflog", "expire", "--expire=now", "--all")
    run_fragment_git(repo, "gc", "--prune=now")
    if _git_commit_exists(repo, result_commit):
        pytest.skip("git retained the test commit after pruning")
    producer_state_path = upstream_stage / "workspace-state.json"
    producer_state_before = producer_state_path.read_bytes()

    locator = WorkspaceFileLocator(
        locator_id="workspace-file-dynamic",
        occurrence_id="build:executor:0:file:future.md",
        node_id="build",
        target="executor_prompt",
        source_class="runtime_dynamic",
        raw_token="{{file:future.md}}",
        raw_path="future.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="future.md",
        workspace_relative_path="future.md",
    )
    plan = make_fragment_plan(context_root).model_copy(
        update={
            "workspace_source": WorkspaceSourceSnapshot(
                worktree_contract=WORKTREE_CONTRACT,
                run_base_commit=parent_commit,
                source_tree=parent_tree,
                object_format="sha1",
                repository_id="repo",
                git_version=run_fragment_git(repo, "--version"),
                git_top_level=repo.as_posix(),
                project_root_relative_path=".",
                active_git_dir=(repo / ".git").as_posix(),
                common_git_dir=(repo / ".git").as_posix(),
                clean_start="strict",
            ),
            "workspace_file_locators": [locator],
            "nodes": [
                make_fragment_plan(context_root).nodes[0],
                make_fragment_plan(context_root)
                .nodes[1]
                .model_copy(
                    update={
                        "workspace_policy": workspace_selection_record(
                            enabled=True,
                            kind="worktree",
                            source_kind="node",
                            source_node_id="input",
                            clean_start="strict",
                            materialization="worktree_checkout",
                        )
                    }
                ),
            ],
            "render_plans": [
                RenderPlan(
                    render_plan_id="build",
                    node_id="build",
                    streams=[
                        RenderStream(
                            target_role=ProviderRole.EXECUTOR,
                            fragments=[
                                Fragment(
                                    fragment_index=0,
                                    kind="workspace_file_locator",
                                    source_role=PromptSegmentRole.SHARED,
                                    locator={
                                        "locator_id": "workspace-file-dynamic",
                                        "source_class": "runtime_dynamic",
                                        "workspace_relative_path": "future.md",
                                    },
                                )
                            ],
                        )
                    ],
                )
            ],
        }
    )
    original_cat_blob = workspace_file_resolution.git_cat_blob

    def collect_after_prune(repo_root: str, object_id: str) -> bytes:
        imported_refs = run_fragment_git(
            repo,
            "for-each-ref",
            "--format=%(objectname)",
            f"refs/crewplane/runs/{plan.run_key_name}/imports",
        ).splitlines()
        assert result_commit in imported_refs
        run_fragment_git(repo, "reflog", "expire", "--expire=now", "--all")
        run_fragment_git(repo, "gc", "--prune=now")
        return original_cat_blob(repo_root, object_id)

    monkeypatch.setattr(
        workspace_file_resolution,
        "git_cat_blob",
        collect_after_prune,
    )

    prompt = assemble_prompt(
        plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
    )

    assert prompt == "bundled dynamic\n"
    assert _git_commit_exists(repo, result_commit)
    assert producer_state_path.read_bytes() == producer_state_before
    assert not tuple(store.logs_dir.glob("workspace-temporary-refs-*.json"))


def _git_commit_exists(repo: Path, object_id: str) -> bool:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), "cat-file", "-e", f"{object_id}^{{commit}}"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0
