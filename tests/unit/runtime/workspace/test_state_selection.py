from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.core.preflight.models import (
    ArtifactContract,
    Fragment,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
    RenderStream,
    WorkspaceFileLocator,
    WorkspaceSourceSnapshot,
)
from crewplane.core.preflight.secrets import FINGERPRINT_PAYLOAD_VERSION
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.policy import WorktreeContract
from crewplane.runtime.execution.workspace_files import (
    WorkspaceCandidateSourceContext,
    dynamic_locator_source,
    dynamic_locator_source_state_path,
    latest_executor_workspace_state,
)
from crewplane.runtime.workspace.state import discard_workspace_lineage
from crewplane.runtime.workspace.state_selection import workspace_state_paths
from crewplane.runtime.workspace.worktree.source_refs import (
    invocation_source_ref,
    required_lineage_state,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.workspace_records import workspace_selection_record


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.stages_dir = root

    def get_stage_dir(self, stage_name: str) -> Path | None:
        path = self.stages_dir / stage_name
        return path if path.is_dir() else None

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        stage_path = request.contract.stage_path
        assert stage_path is not None
        path = self.stages_dir / stage_path
        return path if path.is_dir() else None


def test_required_lineage_state_uses_review_loop_canonical_output(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    canonical_state = stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    stale_state = stage_dir / "workspace-state-aaa-alpha-audit1-round1.json"
    _write_output(stage_dir / "review-audit-round-1" / "alpha_round2.md")
    _write_review_status(stage_dir, "review-audit-round-1/alpha_round2.md")
    _write_state(stale_state, "1" * 40, round_num=1, audit_round_num=1)
    _write_state(canonical_state, "2" * 40, round_num=2, audit_round_num=1)

    assert required_lineage_state(store, _same_node()) == canonical_state


def test_downstream_invocation_source_uses_review_loop_canonical_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    canonical_state = stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    stale_state = stage_dir / "workspace-state-implement-alpha-audit1-round1.json"
    _write_output(stage_dir / "review-audit-round-1" / "alpha_round2.md")
    _write_review_status(stage_dir, "review-audit-round-1/alpha_round2.md")
    _write_state(stale_state, "1" * 40, round_num=1, audit_round_num=1)
    _write_state(canonical_state, "2" * 40, round_num=2, audit_round_num=1)

    source_ref = invocation_source_ref(
        store,
        _downstream_source_plan(tmp_path),
        _downstream_node(),
        workspace_selection_record(
            enabled=True,
            kind="worktree",
            source_kind="node",
            source_node_id="implement",
            clean_start="strict",
            materialization="worktree_checkout",
        ),
        _source_snapshot(tmp_path),
        role_label=ProviderRole.EXECUTOR,
        round_num=1,
        audit_round_num=None,
    )

    assert source_ref.source_kind == "node"
    assert source_ref.source_commit == "2" * 40


def test_required_lineage_state_keeps_review_loop_canonical_over_later_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    canonical_state = stage_dir / "workspace-state-implement-alpha-round2.json"
    later_state = stage_dir / "workspace-state-implement-alpha-round3.json"
    _write_output(stage_dir / "alpha_round2.md")
    _write_review_status(stage_dir, "alpha_round2.md")
    _write_state(canonical_state, "2" * 40, round_num=2, audit_round_num=None)
    _write_state(later_state, "3" * 40, round_num=3, audit_round_num=None)

    assert required_lineage_state(store, _same_node()) == canonical_state


def test_same_node_executor_source_skips_discarded_invalid_candidate(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    valid_state = stage_dir / "workspace-state-implement-alpha-audit1-round1.json"
    invalid_state = stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    _write_state(valid_state, "1" * 40, round_num=1, audit_round_num=1)
    _write_state(invalid_state, "2" * 40, round_num=2, audit_round_num=1)
    discard_workspace_lineage(invalid_state, "invalid_candidate.empty")

    source_ref = invocation_source_ref(
        store,
        _plan_with_locator(tmp_path, _runtime_dynamic_locator()),
        _same_node(),
        workspace_selection_record(
            enabled=True,
            kind="worktree",
            source_kind="project",
            clean_start="strict",
            materialization="worktree_checkout",
        ),
        _source_snapshot(tmp_path),
        role_label=ProviderRole.EXECUTOR,
        round_num=3,
        audit_round_num=1,
    )

    assert source_ref.source_kind == "candidate"
    assert source_ref.source_commit == "1" * 40


def test_dynamic_locator_source_uses_review_loop_canonical_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    canonical_state = stage_dir / "workspace-state-implement-alpha-round2.json"
    later_state = stage_dir / "workspace-state-implement-alpha-round3.json"
    _write_output(stage_dir / "alpha_round2.md")
    _write_review_status(stage_dir, "alpha_round2.md")
    _write_state(canonical_state, "2" * 40, round_num=2, audit_round_num=None)
    _write_state(later_state, "3" * 40, round_num=3, audit_round_num=None)
    locator = _runtime_dynamic_locator()

    state_path = dynamic_locator_source_state_path(
        _plan_with_locator(tmp_path, locator),
        store,
        locator,
        workspace_candidate_source=False,
    )

    assert state_path == canonical_state


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
    _write_output(stage_dir / "review-audit-round-1" / "alpha_round1.md")
    _write_review_status(stage_dir, "review-audit-round-1/alpha_round1.md")
    _write_state(stale_canonical_state, "1" * 40, round_num=1, audit_round_num=1)
    _write_state(previous_executor_state, "2" * 40, round_num=2, audit_round_num=1)
    locator = _project_then_candidate_locator()

    state_path = dynamic_locator_source_state_path(
        _plan_with_locator(tmp_path, locator),
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
    _write_output(stage_dir / "review-audit-round-1" / "alpha_round1.md")
    _write_review_status(stage_dir, "review-audit-round-1/alpha_round1.md")
    _write_state(stale_canonical_state, "1" * 40, round_num=1, audit_round_num=1)
    _write_state(current_executor_state, "2" * 40, round_num=2, audit_round_num=1)
    locator = _runtime_dynamic_locator("reviewer_prompt")

    state_path = dynamic_locator_source_state_path(
        _plan_with_locator(tmp_path, locator),
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
    _write_state(previous_executor_state, "2" * 40, round_num=2, audit_round_num=1)
    locator = _runtime_dynamic_locator("reviewer_prompt")

    state_path = dynamic_locator_source_state_path(
        _plan_with_locator(tmp_path, locator),
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
    locator = _runtime_dynamic_locator("reviewer_prompt")

    source = dynamic_locator_source(
        _plan_with_locator(tmp_path, locator),
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
    _write_state(upstream_state, "9" * 40, round_num=1, audit_round_num=None)
    locator = _runtime_dynamic_locator("reviewer_prompt")
    node = _same_node().model_copy(
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
    upstream = _same_node().model_copy(
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
    base_plan = _plan_with_locator(tmp_path, locator)
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
    locator = _runtime_dynamic_locator("reviewer_prompt")

    with pytest.raises(RuntimeError, match="no matching executor state"):
        dynamic_locator_source_state_path(
            _plan_with_locator(tmp_path, locator),
            store,
            locator,
            workspace_candidate_context=WorkspaceCandidateSourceContext(
                role_label=ProviderRole.REVIEWER,
                round_num=1,
                audit_round_num=None,
            ),
        )


def test_required_lineage_state_prefers_latest_executor_without_review_status(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    first_state = stage_dir / "workspace-state.json"
    latest_state = stage_dir / "workspace-state-implement-alpha-round2.json"
    _write_state(first_state, "1" * 40, round_num=1, audit_round_num=None)
    _write_state(latest_state, "2" * 40, round_num=2, audit_round_num=None)

    assert required_lineage_state(store, _same_node()) == latest_state


def test_required_lineage_state_resolves_seeded_audit_copy_to_previous_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    previous_state = stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    _write_output(stage_dir / "review-audit-round-2" / "alpha_round1.md")
    _write_review_status(stage_dir, "review-audit-round-2/alpha_round1.md")
    _write_state(previous_state, "2" * 40, round_num=2, audit_round_num=1)

    assert required_lineage_state(store, _same_node()) == previous_state


def test_required_lineage_state_fails_when_canonical_status_has_no_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    _write_output(stage_dir / "alpha_round2.md")
    _write_review_status(stage_dir, "alpha_round2.md")

    with pytest.raises(RuntimeError, match="no matching succeeded workspace state"):
        required_lineage_state(store, _same_node())


def test_latest_executor_workspace_state_uses_payload_order_not_filename_order(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "build"
    _write_state(
        stage_dir / "workspace-state-z-alpha-round9.json",
        "9" * 40,
        round_num=9,
        audit_round_num=None,
    )
    _write_state(
        stage_dir / "workspace-state-a-alpha-round10.json",
        "a" * 40,
        round_num=10,
        audit_round_num=None,
    )

    build_node = _same_node().model_copy(
        update={
            "id": "build",
            "artifact_contract": ArtifactContract(
                stage_path="build",
                output_path="build.md",
                log_path="build/logs",
                result_path="build.md",
            ),
        }
    )
    state = latest_executor_workspace_state(store, build_node)

    assert state["result"]["result_commit"] == "a" * 40


def test_workspace_state_paths_rejects_symlink_state(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    outside = tmp_path / "outside-state.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        (stage_dir / "workspace-state.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match="Unsafe workspace state artifact"):
        workspace_state_paths(stage_dir)


def test_workspace_state_paths_rejects_hardlinked_state(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    outside = tmp_path / "outside-state.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        os.link(outside, stage_dir / "workspace-state.json")
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    with pytest.raises(RuntimeError, match="Unsafe workspace state artifact"):
        workspace_state_paths(stage_dir)


def _write_output(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("candidate\n", encoding="utf-8")


def _write_review_status(stage_dir: Path, canonical_path: str) -> None:
    status_dir = stage_dir / "review-state"
    status_dir.mkdir(parents=True, exist_ok=True)
    output_bytes = (stage_dir / canonical_path).read_bytes()
    round_match = re.search(r"_round(\d+)\.md$", canonical_path)
    assert round_match is not None
    round_num = int(round_match.group(1))
    audit_match = re.fullmatch(
        r"review-audit-round-(\d+)",
        Path(canonical_path).parent.name,
    )
    audit_round_num = int(audit_match.group(1)) if audit_match else None
    payload = {
        "node_id": "implement",
        "executed_audit_rounds": audit_round_num or 1,
        "attempted_local_round_num": round_num,
        "final_local_round_num": round_num,
        "invalid_candidate_round_count": 0,
        "no_progress_round_count": 0,
        "artifact_drift_warning_count": 0,
        "consensus_reached": True,
        "continued_after_consensus_exhaustion": False,
        "canonical_executor_outputs": [
            {
                "task_id": "alpha",
                "provider": "codex",
                "role": "executor",
                "path": canonical_path,
                "sha256": hashlib.sha256(output_bytes).hexdigest(),
                "size_bytes": len(output_bytes),
                "audit_round_num": audit_round_num,
                "round_num": round_num,
            }
        ],
        "reviewer_outputs": [],
    }
    (status_dir / "review-loop-status.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _write_state(
    path: Path,
    result_commit: str,
    round_num: int,
    audit_round_num: int | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "succeeded",
        "role": "executor",
        "task_id": "alpha",
        "round_num": round_num,
        "audit_round_num": audit_round_num,
        "workspace": {"lineage_producer": True},
        "result": {
            "result_commit": result_commit,
            "result_tree": "b" * 40,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _downstream_node() -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="verify",
        mode="sequential",
        dependencies=["implement"],
        render_plan_id="verify",
        provider_records=[
            ProviderRecord(
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha",
                agent_config_key="alpha",
                invoker_alias="mock",
                agent_config_signature="agent",
                invoker_config_signature="invoker",
            )
        ],
        workspace_policy=workspace_selection_record(
            enabled=True,
            kind="worktree",
            source_kind="node",
            source_node_id="implement",
            clean_start="strict",
            materialization="worktree_checkout",
        ),
        artifact_contract=ArtifactContract(
            stage_path="verify",
            output_path="verify.md",
            log_path="verify/logs",
            result_path="verify.md",
        ),
    )


def _same_node() -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="implement",
        mode="sequential",
        render_plan_id="implement",
        provider_records=[
            ProviderRecord(
                provider="codex",
                role=ProviderRole.EXECUTOR,
                task_id="alpha",
                agent_config_key="alpha",
                invoker_alias="mock",
                agent_config_signature="agent",
                invoker_config_signature="invoker",
            )
        ],
        workspace_policy=workspace_selection_record(
            enabled=True,
            kind="worktree",
            source_kind="project",
            clean_start="strict",
            materialization="worktree_checkout",
        ),
        artifact_contract=ArtifactContract(
            stage_path="implement",
            output_path="implement.md",
            log_path="implement/logs",
            result_path="implement.md",
        ),
    )


def _downstream_source_plan(tmp_path: Path) -> PreflightExecutionPlan:
    plan = _plan_with_locator(tmp_path, _runtime_dynamic_locator())
    downstream = _downstream_node()
    return plan.model_copy(
        update={
            "execution_order": ["implement", "verify"],
            "nodes": [_same_node(), downstream],
            "render_plans": [
                RenderPlan(render_plan_id="implement", node_id="implement"),
                RenderPlan(render_plan_id="verify", node_id="verify"),
            ],
            "workspace_file_locators": [],
        }
    )


def _runtime_dynamic_locator(target: str = "reviewer_prompt") -> WorkspaceFileLocator:
    return WorkspaceFileLocator(
        locator_id=f"implement:{target}:file:README.md",
        occurrence_id=f"implement:{target}:file:README.md",
        node_id="implement",
        target=target,
        source_class="runtime_dynamic",
        raw_token="{{file:README.md}}",
        raw_path="README.md",
        source_root="/repo",
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="README.md",
        workspace_relative_path="README.md",
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


def _plan_with_locator(
    tmp_path: Path,
    locator: WorkspaceFileLocator,
) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        plan_schema_version=SCHEMA_VERSION,
        run_id="run",
        run_key_name="run",
        project_root=tmp_path.as_posix(),
        context_root=tmp_path.as_posix(),
        manifest_root=(tmp_path / "manifests").as_posix(),
        created_at="2026-06-18T00:00:00",
        workflow_name="workflow",
        workflow_signature="workflow-signature",
        execution_order=["implement"],
        nodes=[_same_node()],
        render_plans=[
            RenderPlan(
                render_plan_id="implement",
                node_id="implement",
                streams=[
                    RenderStream(
                        target_role=(
                            ProviderRole.REVIEWER
                            if locator.target == "reviewer_prompt"
                            else ProviderRole.EXECUTOR
                        ),
                        fragments=[
                            Fragment(
                                fragment_index=0,
                                kind="workspace_file_locator",
                                source_role=PromptSegmentRole.SHARED,
                                locator={
                                    "locator_id": locator.locator_id,
                                    "source_class": locator.source_class.value,
                                    "workspace_relative_path": (
                                        locator.workspace_relative_path
                                    ),
                                },
                            )
                        ],
                    )
                ],
            )
        ],
        static_resources=[],
        workspace_file_locators=[locator],
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        effective_runtime_config_signature="runtime-signature",
        workspace_source=_source_snapshot(tmp_path),
        fingerprint_metadata={"payload_version": FINGERPRINT_PAYLOAD_VERSION},
    )


def _source_snapshot(tmp_path: Path) -> WorkspaceSourceSnapshot:
    return WorkspaceSourceSnapshot(
        worktree_contract=WorktreeContract(),
        run_base_commit="0" * 40,
        source_tree="0" * 40,
        object_format="sha1",
        repository_id="repo",
        git_version="git version 2.44.0",
        git_top_level=tmp_path.as_posix(),
        project_root_relative_path=".",
        active_git_dir=(tmp_path / ".git").as_posix(),
        common_git_dir=(tmp_path / ".git").as_posix(),
        clean_start="strict",
    )
