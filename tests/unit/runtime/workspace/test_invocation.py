from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    ProviderRecord,
    WorkspaceFileLocator,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.naming import safe_ref_component
from crewplane.runtime.execution.review_loop.workspace_state_paths import (
    workspace_artifact_allowed_paths,
)
from crewplane.runtime.execution.workspace_files import (
    dynamic_locator_source_state_path,
)
from crewplane.runtime.workspace.invocation import (
    invocation_slug,
    workspace_state_path,
)
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_records import workspace_selection_record
from tests.helpers.workspace_service import create_git_repo, workspace_plan


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.stages_dir = root

    def get_stage_dir(self, stage_name: str) -> Path | None:
        path = self.stages_dir / stage_name
        return path if path.is_dir() else None

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        stage_path = request.contract.stage_path
        if stage_path is None:
            return None
        path = self.stages_dir / stage_path
        return path if path.is_dir() else None

    def create_stage_dir(self, stage_name: str) -> Path:
        path = self.stages_dir / stage_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def create_node_dir(self, request: NodeArtifactRequest) -> Path:
        stage_path = request.contract.stage_path
        if stage_path is None:
            raise ValueError("missing stage path")
        path = self.stages_dir / stage_path
        path.mkdir(parents=True, exist_ok=True)
        return path


def test_single_provider_workspace_state_path_is_round_specific_after_round_one(
    tmp_path: Path,
) -> None:
    output = ArtifactStore(tmp_path)
    node = _node()
    round_one_slug = invocation_slug("implement", "alpha", None, 1)
    round_two_slug = invocation_slug("implement", "alpha", None, 2)

    assert workspace_state_path(output, node, round_one_slug, None, 1) == (
        tmp_path / "implement" / "workspace-state.json"
    )
    assert workspace_state_path(output, node, round_two_slug, None, 2) == (
        tmp_path / "implement" / f"workspace-state-{round_two_slug}.json"
    )


def test_invocation_slug_preserves_long_node_identity() -> None:
    common_prefix = "node-" + ("x" * 180)
    first = invocation_slug(f"{common_prefix}-first", "alpha", None, 1)
    second = invocation_slug(f"{common_prefix}-second", "alpha", None, 1)

    assert len(first) <= 160
    assert len(second) <= 160
    assert first != second


@pytest.mark.parametrize(
    ("first_identity", "second_identity"),
    [
        (("a", "alpha"), (".a", "alpha")),
        (("a.b", "alpha"), ("a..b", "alpha")),
        (("a-alpha", "beta"), ("a", "alpha-beta")),
    ],
)
def test_invocation_slug_preserves_identity_after_path_and_ref_normalization(
    first_identity: tuple[str, str],
    second_identity: tuple[str, str],
) -> None:
    first = invocation_slug(*first_identity, None, 1)
    second = invocation_slug(*second_identity, None, 1)

    assert first != second
    assert safe_ref_component(first) != safe_ref_component(second)


def test_workspace_artifact_allowed_paths_include_setup_outputs(
    tmp_path: Path,
) -> None:
    output = ArtifactStore(tmp_path)
    node = _node_with_setup()

    allowed_paths = workspace_artifact_allowed_paths(
        output,
        node,
        task_id="alpha",
        role_label=ProviderRole.EXECUTOR,
        audit_round_num=None,
        round_num=2,
    )

    stage_dir = tmp_path / "implement"
    slug = invocation_slug("implement", "alpha", None, 2)
    assert stage_dir / f"workspace-state-{slug}.json" in allowed_paths
    assert (
        stage_dir / "workspace-setup" / f"workspace-state-{slug}.json"
    ) in allowed_paths
    assert (
        stage_dir / "workspace-setup" / f"workspace-state-{slug}.log"
    ) in allowed_paths


def test_executor_locator_switches_to_candidate_only_when_requested(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    output = ArtifactStore(tmp_path / "stages")
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    upstream_state = (
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    current_state = (
        output.create_node_dir(node_artifact_request("verify"))
        / "workspace-state-verify-alpha-round1.json"
    )
    _write_lineage_state(upstream_state, "1" * 40)
    _write_lineage_state(current_state, "2" * 40)
    verify_node = _node("verify", source_node_id="implement")
    plan = plan.model_copy(update={"nodes": [plan.nodes[0], verify_node]})
    locator = WorkspaceFileLocator(
        locator_id="locator",
        occurrence_id="occurrence",
        node_id="verify",
        target="executor_prompt",
        source_class="runtime_dynamic",
        raw_token="{{file:generated.md}}",
        raw_path="generated.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="generated.md",
        workspace_relative_path="generated.md",
    )

    assert (
        dynamic_locator_source_state_path(plan, output, locator, False)
        == upstream_state
    )
    assert (
        dynamic_locator_source_state_path(plan, output, locator, True) == current_state
    )


def _node(
    node_id: str = "implement",
    source_node_id: str | None = None,
) -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id=node_id,
        mode="sequential",
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
            source_kind="node" if source_node_id is not None else "project",
            source_node_id=source_node_id,
            clean_start="strict",
            materialization="worktree_checkout",
        ),
        artifact_contract=ArtifactContract(
            stage_path=node_id,
            output_path=f"{node_id}/output.md",
        ),
    )


def _node_with_setup() -> PreflightExecutionNode:
    node = _node()
    policy = node.workspace_policy
    assert policy is not None
    return node.model_copy(
        update={
            "workspace_policy": policy.model_copy(
                update={
                    "setup": WorkspaceSetupRecord(
                        profile_name="bootstrap",
                        commands=[
                            WorkspaceSetupCommandRecord(
                                argv=["python", "-c", "print('setup')"],
                                command_index=0,
                            )
                        ],
                    )
                }
            )
        }
    )


def _write_lineage_state(path: Path, result_commit: str) -> None:
    payload = {
        "status": "succeeded",
        "role": "executor",
        "task_id": "alpha",
        "round_num": 1,
        "audit_round_num": None,
        "workspace": {"lineage_producer": True},
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": "0" * 40,
            "tree": "0" * 40,
            "candidate_sequence": None,
        },
        "result": {
            "result_commit": result_commit,
            "result_tree": "3" * 40,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
