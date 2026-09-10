from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

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
from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.core.workspace.policy import WorktreeContract
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


def write_selection_output(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("candidate\n", encoding="utf-8")


def write_selection_review_status(stage_dir: Path, canonical_path: str) -> None:
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


def write_selection_state(
    path: Path,
    result_commit: str,
    round_num: int,
    audit_round_num: int | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    node_id = path.parent.name
    run_id = "run-001"
    run_key_name = "workspace-run-001"
    slug = invocation_slug(node_id, "alpha", audit_round_num, round_num)
    candidate_ref = f"refs/crewplane/runs/{run_key_name}/{node_id}/{slug}/candidate"
    result_ref = f"refs/crewplane/runs/{run_key_name}/{node_id}/{slug}/result"
    payload = {
        "version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_key_name": run_key_name,
        "workflow_name": "workspace",
        "workflow_signature": "workflow-signature",
        "node_id": node_id,
        "status": "succeeded",
        "role": "executor",
        "task_id": "alpha",
        "provider": "codex",
        "round_num": round_num,
        "audit_round_num": audit_round_num,
        "workspace_kind": "worktree",
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": {"mode": "blob_exact", "schema_version": SCHEMA_VERSION},
        "git": {
            "object_format": "sha1",
            "repo_id": "repo-id",
            "run_base_commit": "c" * 40,
            "source_tree": "d" * 40,
            "git_top_level": path.parent.parent.as_posix(),
            "active_git_dir": (path.parent.parent / ".git").as_posix(),
            "common_git_dir": (path.parent.parent / ".git").as_posix(),
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": "c" * 40,
            "tree": "d" * 40,
            "candidate_sequence": None,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": "c" * 40,
            "source_tree": "d" * 40,
            "candidate_sequence": None,
        },
        "workspace": {
            "materialization": "worktree_checkout",
            "writable": True,
            "lineage_producer": True,
            "retention": "retained",
            "reuse_generation": 1,
        },
        "execution": {
            "effective_cwd": (path.parent / "checkout").as_posix(),
            "worktree_git_dir": (path.parent.parent / ".git/worktrees/test").as_posix(),
        },
        "process_drain": {"status": "confirmed"},
        "result": {
            "candidate_commit": result_commit,
            "result_commit": result_commit,
            "candidate_tree": "b" * 40,
            "result_tree": "b" * 40,
            "changed_path_count": 1,
        },
        "refs": {"candidate": candidate_ref, "result": result_ref},
        "bundle": {
            "path": f"{node_id}/workspace-bundles/alpha.bundle",
            "sha256": "e" * 64,
            "size_bytes": 1,
            "verified": True,
        },
        "ref_publication": {
            "phase": "published",
            "repository_id": "repo-id",
            "run_id": run_id,
            "run_key_name": run_key_name,
            "node_id": node_id,
            "task_id": "alpha",
            "role": "executor",
            "round_num": round_num,
            "audit_round_num": audit_round_num,
            "destinations": {
                "candidate": {
                    "name": candidate_ref,
                    "target_oid": result_commit,
                    "expected_old_oid": None,
                },
                "result": {
                    "name": result_ref,
                    "target_oid": result_commit,
                    "expected_old_oid": None,
                },
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def same_selection_node() -> PreflightExecutionNode:
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


def runtime_dynamic_locator(target: str = "reviewer_prompt") -> WorkspaceFileLocator:
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


def selection_plan_with_locator(
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
        nodes=[same_selection_node()],
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
        workspace_source=selection_source_snapshot(tmp_path),
        fingerprint_metadata={"payload_version": FINGERPRINT_PAYLOAD_VERSION},
    )


def selection_source_snapshot(tmp_path: Path) -> WorkspaceSourceSnapshot:
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
