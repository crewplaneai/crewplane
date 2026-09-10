from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from crewplane.core.preflight.models import (
    ArtifactContract,
    RenderPlan,
    WorkspaceFileLocator,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.workspace_files import (
    WorkspaceCandidateSourceContext,
    dynamic_locator_source,
    dynamic_locator_source_state_path,
)
from tests.helpers.workspace_records import workspace_selection_record
from tests.unit.runtime.workspace.state_selection_support import (
    ArtifactStore,
    runtime_dynamic_locator,
    same_selection_node,
    selection_plan_with_locator,
    write_selection_output,
    write_selection_review_status,
    write_selection_state,
)


def test_dynamic_locator_source_uses_review_loop_canonical_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    canonical_state = stage_dir / "workspace-state-implement-alpha-round2.json"
    later_state = stage_dir / "workspace-state-implement-alpha-round3.json"
    write_selection_output(stage_dir / "alpha_round2.md")
    write_selection_review_status(stage_dir, "alpha_round2.md")
    write_selection_state(canonical_state, "2" * 40, round_num=2, audit_round_num=None)
    write_selection_state(later_state, "3" * 40, round_num=3, audit_round_num=None)
    locator = runtime_dynamic_locator()

    state_path = dynamic_locator_source_state_path(
        selection_plan_with_locator(tmp_path, locator),
        store,
        locator,
        workspace_candidate_source=False,
    )

    assert state_path == canonical_state


def test_dynamic_locator_source_state_path_requires_workspace_policy(
    tmp_path: Path,
) -> None:
    locator = runtime_dynamic_locator()
    plan = selection_plan_with_locator(tmp_path, locator).model_copy(
        update={
            "nodes": [
                same_selection_node().model_copy(update={"workspace_policy": None})
            ]
        }
    )

    with pytest.raises(RuntimeError, match="has no workspace policy"):
        dynamic_locator_source_state_path(plan, ArtifactStore(tmp_path), locator)


def test_dynamic_locator_source_state_path_requires_stage_directory(
    tmp_path: Path,
) -> None:
    locator = runtime_dynamic_locator()

    with pytest.raises(RuntimeError, match="has no stage directory"):
        dynamic_locator_source_state_path(
            selection_plan_with_locator(tmp_path, locator),
            ArtifactStore(tmp_path),
            locator,
        )


def test_dynamic_locator_source_state_path_requires_succeeded_state(
    tmp_path: Path,
) -> None:
    locator = runtime_dynamic_locator()
    (tmp_path / "implement").mkdir()

    with pytest.raises(RuntimeError, match="has no succeeded executor state"):
        dynamic_locator_source_state_path(
            selection_plan_with_locator(tmp_path, locator),
            ArtifactStore(tmp_path),
            locator,
        )


def test_dynamic_locator_source_context_uses_previous_executor_candidate(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    stale_canonical_state = (
        stage_dir / "workspace-state-implement-alpha-audit1-round1.json"
    )
    previous_executor_state = (
        stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    )
    write_selection_output(stage_dir / "review-audit-round-1" / "alpha_round1.md")
    write_selection_review_status(stage_dir, "review-audit-round-1/alpha_round1.md")
    write_selection_state(
        stale_canonical_state, "1" * 40, round_num=1, audit_round_num=1
    )
    write_selection_state(
        previous_executor_state, "2" * 40, round_num=2, audit_round_num=1
    )
    locator = _project_then_candidate_locator()

    state_path = dynamic_locator_source_state_path(
        selection_plan_with_locator(tmp_path, locator),
        store,
        locator,
        workspace_candidate_source=True,
        workspace_candidate_context=WorkspaceCandidateSourceContext(
            role_label=ProviderRole.EXECUTOR,
            round_num=3,
            audit_round_num=1,
        ),
    )

    assert state_path == previous_executor_state


def test_dynamic_locator_source_context_uses_current_reviewer_candidate(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    stale_canonical_state = (
        stage_dir / "workspace-state-implement-alpha-audit1-round1.json"
    )
    current_executor_state = (
        stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    )
    write_selection_output(stage_dir / "review-audit-round-1" / "alpha_round1.md")
    write_selection_review_status(stage_dir, "review-audit-round-1/alpha_round1.md")
    write_selection_state(
        stale_canonical_state, "1" * 40, round_num=1, audit_round_num=1
    )
    write_selection_state(
        current_executor_state, "2" * 40, round_num=2, audit_round_num=1
    )
    locator = runtime_dynamic_locator("reviewer_prompt")

    state_path = dynamic_locator_source_state_path(
        selection_plan_with_locator(tmp_path, locator),
        store,
        locator,
        workspace_candidate_source=False,
        workspace_candidate_context=WorkspaceCandidateSourceContext(
            role_label=ProviderRole.REVIEWER,
            round_num=2,
            audit_round_num=1,
        ),
    )

    assert state_path == current_executor_state


def test_reviewer_dynamic_locator_falls_back_to_prior_seeded_audit_candidate(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    previous_executor_state = (
        stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    )
    write_selection_state(
        previous_executor_state, "2" * 40, round_num=2, audit_round_num=1
    )
    locator = runtime_dynamic_locator("reviewer_prompt")

    state_path = dynamic_locator_source_state_path(
        selection_plan_with_locator(tmp_path, locator),
        store,
        locator,
        workspace_candidate_source=False,
        workspace_candidate_context=WorkspaceCandidateSourceContext(
            role_label=ProviderRole.REVIEWER,
            round_num=2,
            audit_round_num=2,
        ),
    )

    assert state_path == previous_executor_state


def test_initial_pre_review_dynamic_locator_uses_project_source(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    locator = runtime_dynamic_locator("reviewer_prompt")

    source = dynamic_locator_source(
        selection_plan_with_locator(tmp_path, locator),
        store,
        locator,
        workspace_candidate_context=WorkspaceCandidateSourceContext(
            role_label=ProviderRole.REVIEWER,
            round_num=0,
            audit_round_num=None,
            phase="initial_pre_review",
        ),
    )

    assert source.source_kind == "project"
    assert source.source_commit == "0" * 40


def test_initial_pre_review_dynamic_locator_uses_upstream_source(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    upstream_state = tmp_path / "upstream" / "workspace-state.json"
    write_selection_state(upstream_state, "9" * 40, round_num=1, audit_round_num=None)
    locator = runtime_dynamic_locator("reviewer_prompt")
    node = same_selection_node().model_copy(
        update={
            "dependencies": ["upstream"],
            "workspace_policy": workspace_selection_record(
                enabled=True,
                kind="worktree",
                source_kind="node",
                source_node_id="upstream",
                clean_start="strict",
                materialization="worktree_checkout",
            ),
        }
    )
    upstream = same_selection_node().model_copy(
        update={
            "id": "upstream",
            "render_plan_id": "upstream",
            "artifact_contract": ArtifactContract(
                stage_path="upstream",
                output_path="upstream.md",
                log_path="upstream/logs",
                result_path="upstream.md",
            ),
        }
    )
    base_plan = selection_plan_with_locator(tmp_path, locator)
    plan = base_plan.model_copy(
        update={
            "execution_order": ["upstream", "implement"],
            "nodes": [upstream, node],
            "render_plans": [
                RenderPlan(render_plan_id="upstream", node_id="upstream"),
                *base_plan.render_plans,
            ],
        }
    )

    source = dynamic_locator_source(
        plan,
        store,
        locator,
        workspace_candidate_context=WorkspaceCandidateSourceContext(
            role_label=ProviderRole.REVIEWER,
            round_num=0,
            audit_round_num=None,
            phase="initial_pre_review",
        ),
    )

    assert source.source_kind == "node"
    assert source.source_commit == "9" * 40


def test_candidate_review_dynamic_locator_still_requires_executor_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    locator = runtime_dynamic_locator("reviewer_prompt")

    with pytest.raises(RuntimeError, match="no matching executor state"):
        dynamic_locator_source_state_path(
            selection_plan_with_locator(tmp_path, locator),
            store,
            locator,
            workspace_candidate_context=WorkspaceCandidateSourceContext(
                role_label=ProviderRole.REVIEWER,
                round_num=1,
                audit_round_num=None,
            ),
        )


def _project_then_candidate_locator() -> WorkspaceFileLocator:
    payload = b"project initial\n"
    return WorkspaceFileLocator(
        locator_id="implement:executor_prompt:file:README.md",
        content_ref="workspace-files/implement-executor-readme.txt",
        occurrence_id="implement:executor_prompt:file:README.md",
        node_id="implement",
        target="executor_prompt",
        source_class="project_initial_then_candidate",
        raw_token="{{file:README.md}}",
        raw_path="README.md",
        source_root="/repo",
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="README.md",
        workspace_relative_path="README.md",
        git_blob="0" * 40,
        git_file_mode="100644",
        byte_size=len(payload),
        canonical_blob_sha256=hashlib.sha256(payload).hexdigest(),
        literal_path_verified=True,
        utf8_validated=True,
    )
