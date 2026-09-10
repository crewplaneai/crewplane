from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from crewplane.core.preflight.models import (
    Fragment,
    PreflightExecutionPlan,
    RenderPlan,
    RenderStream,
    WorkspaceFileLocator,
    WorkspaceSourceSnapshot,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.fragment_assembler import assemble_prompt
from crewplane.runtime.execution.workspace_files import (
    WorkspaceCandidateSourceContext,
    resolve_workspace_file,
)
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


def test_initial_pre_review_reads_project_initial_runtime_dynamic_workspace_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_fragment_git(repo, "init")
    run_fragment_git(repo, "config", "user.name", "Crewplane Test")
    run_fragment_git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("project initial\n", encoding="utf-8")
    run_fragment_git(repo, "add", "README.md")
    run_fragment_git(repo, "commit", "-m", "base")
    run_base_commit = run_fragment_git(repo, "rev-parse", "HEAD^{commit}")
    source_tree = run_fragment_git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = FragmentArtifactStore(tmp_path)
    locator = _reviewer_runtime_dynamic_locator(repo)
    plan = _reviewer_workspace_plan(
        context_root,
        repo,
        run_base_commit,
        source_tree,
        locator,
    )
    context = WorkspaceCandidateSourceContext(
        role_label=ProviderRole.REVIEWER,
        round_num=0,
        audit_round_num=None,
        phase="initial_pre_review",
    )

    prompt = assemble_prompt(
        plan,
        plan.nodes[1],
        ProviderRole.REVIEWER,
        store,
        SecretContext(),
        workspace_candidate_context=context,
    )
    resolved = resolve_workspace_file(
        plan,
        store,
        locator.locator_id,
        workspace_candidate_context=context,
    )

    assert prompt == "project initial\n"
    assert resolved.source_ref is not None
    assert resolved.source_ref.source_kind == "project"


def test_initial_pre_review_reads_upstream_runtime_dynamic_workspace_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_fragment_git(repo, "init")
    run_fragment_git(repo, "config", "user.name", "Crewplane Test")
    run_fragment_git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("project initial\n", encoding="utf-8")
    run_fragment_git(repo, "add", "README.md")
    run_fragment_git(repo, "commit", "-m", "base")
    run_base_commit = run_fragment_git(repo, "rev-parse", "HEAD^{commit}")
    source_tree = run_fragment_git(repo, "rev-parse", "HEAD^{tree}")
    (repo / "README.md").write_text("upstream lineage\n", encoding="utf-8")
    run_fragment_git(repo, "add", "README.md")
    run_fragment_git(repo, "commit", "-m", "upstream")
    upstream_commit = run_fragment_git(repo, "rev-parse", "HEAD^{commit}")
    upstream_tree = run_fragment_git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = FragmentArtifactStore(tmp_path)
    write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=store.stages_dir / "input-stage",
        source_commit=run_base_commit,
        source_tree=source_tree,
        result_commit=upstream_commit,
        result_tree=upstream_tree,
        bundle_name="input.bundle",
        extra_payload={"node_id": "input"},
    )
    locator = _reviewer_runtime_dynamic_locator(repo)
    plan = _reviewer_workspace_plan(
        context_root,
        repo,
        run_base_commit,
        source_tree,
        locator,
        source_kind="node",
        source_node_id="input",
    )

    prompt = assemble_prompt(
        plan,
        plan.nodes[1],
        ProviderRole.REVIEWER,
        store,
        SecretContext(),
        workspace_candidate_context=WorkspaceCandidateSourceContext(
            role_label=ProviderRole.REVIEWER,
            round_num=0,
            audit_round_num=None,
            phase="initial_pre_review",
        ),
    )

    assert prompt == "upstream lineage\n"


def test_normal_candidate_review_rejects_missing_runtime_dynamic_candidate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_fragment_git(repo, "init")
    run_fragment_git(repo, "config", "user.name", "Crewplane Test")
    run_fragment_git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("project initial\n", encoding="utf-8")
    run_fragment_git(repo, "add", "README.md")
    run_fragment_git(repo, "commit", "-m", "base")
    run_base_commit = run_fragment_git(repo, "rev-parse", "HEAD^{commit}")
    source_tree = run_fragment_git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = FragmentArtifactStore(tmp_path)
    locator = _reviewer_runtime_dynamic_locator(repo)
    plan = _reviewer_workspace_plan(
        context_root,
        repo,
        run_base_commit,
        source_tree,
        locator,
    )

    with pytest.raises(RuntimeError, match="no matching executor state"):
        assemble_prompt(
            plan,
            plan.nodes[1],
            ProviderRole.REVIEWER,
            store,
            SecretContext(),
            workspace_candidate_context=WorkspaceCandidateSourceContext(
                role_label=ProviderRole.REVIEWER,
                round_num=1,
                audit_round_num=None,
            ),
        )


def test_assemble_prompt_reads_after_candidate_workspace_locator_from_candidate(
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
    (repo / "future.md").write_text("candidate\n", encoding="utf-8")
    run_fragment_git(repo, "add", "future.md")
    run_fragment_git(repo, "commit", "-m", "candidate")
    result_commit = run_fragment_git(repo, "rev-parse", "HEAD^{commit}")
    result_tree = run_fragment_git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    content_ref = "workspace-files/workspace-file-after-candidate.txt"
    preflight_file = context_root / "preflight" / content_ref
    preflight_file.parent.mkdir(parents=True)
    preflight_file.write_text("base\n", encoding="utf-8")
    store = FragmentArtifactStore(tmp_path)
    build_stage = store.stages_dir / "build-stage"
    write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=build_stage,
        source_commit=source_commit,
        source_tree=source_tree,
        result_commit=result_commit,
        result_tree=result_tree,
        bundle_name="build.bundle",
        extra_payload={
            "node_id": "build",
            "task_id": "alpha",
            "round_num": 1,
            "audit_round_num": None,
        },
    )
    locator = WorkspaceFileLocator(
        locator_id="workspace-file-after-candidate",
        content_ref=content_ref,
        occurrence_id="build:executor:0:file:future.md",
        node_id="build",
        target="executor_prompt",
        source_class="project_initial_then_candidate",
        raw_token="{{file:future.md}}",
        raw_path="future.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="future.md",
        workspace_relative_path="future.md",
        git_blob="a" * 40,
        git_file_mode="100644",
        byte_size=len("base\n"),
        canonical_blob_sha256=hashlib.sha256(b"base\n").hexdigest(),
        literal_path_verified=True,
        utf8_validated=True,
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
                                        "locator_id": "workspace-file-after-candidate",
                                        "source_class": "project_initial_then_candidate",
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

    assert (
        assemble_prompt(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )
        == "base\n"
    )
    assert (
        assemble_prompt(
            plan,
            plan.nodes[1],
            ProviderRole.EXECUTOR,
            store,
            SecretContext(),
            workspace_candidate_source=True,
        )
        == "candidate\n"
    )


def _reviewer_runtime_dynamic_locator(repo: Path) -> WorkspaceFileLocator:
    return WorkspaceFileLocator(
        locator_id="workspace-file-reviewer-dynamic",
        occurrence_id="build:reviewer:0:file:README.md",
        node_id="build",
        target="reviewer_prompt",
        source_class="runtime_dynamic",
        raw_token="{{file:README.md}}",
        raw_path="README.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="README.md",
        workspace_relative_path="README.md",
    )


def _reviewer_workspace_plan(
    context_root: Path,
    repo: Path,
    run_base_commit: str,
    source_tree: str,
    locator: WorkspaceFileLocator,
    source_kind: str = "project",
    source_node_id: str | None = None,
) -> PreflightExecutionPlan:
    base_plan = make_fragment_plan(context_root)
    return base_plan.model_copy(
        update={
            "workspace_source": WorkspaceSourceSnapshot(
                worktree_contract=WORKTREE_CONTRACT,
                run_base_commit=run_base_commit,
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
                base_plan.nodes[0],
                base_plan.nodes[1].model_copy(
                    update={
                        "workspace_policy": workspace_selection_record(
                            enabled=True,
                            kind="worktree",
                            source_kind=source_kind,
                            source_node_id=source_node_id,
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
                            target_role=ProviderRole.REVIEWER,
                            fragments=[
                                Fragment(
                                    fragment_index=0,
                                    kind="workspace_file_locator",
                                    source_role=PromptSegmentRole.REVIEWER,
                                    locator={
                                        "locator_id": locator.locator_id,
                                        "source_class": "runtime_dynamic",
                                        "workspace_relative_path": "README.md",
                                    },
                                )
                            ],
                        )
                    ],
                )
            ],
        }
    )
