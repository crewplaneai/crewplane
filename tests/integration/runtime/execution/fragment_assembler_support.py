from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest, VerifiedNodeArtifact
from crewplane.core.preflight.dependency_edges import dependency_signature
from crewplane.core.preflight.models import (
    ArtifactContract,
    DependencyEdge,
    ExecutionPolicy,
    Fragment,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
    RenderStream,
)
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.version import SCHEMA_VERSION
from tests.helpers.workspace_records import (
    WORKTREE_CONTRACT,
)


class FragmentArtifactStore:
    run_id = "run"
    task_name = "demo"
    log_cli_output = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.stages_dir = root / "stages"
        self.results_dir = root / "results"
        self.logs_dir = root / "logs"
        self.stages_dir.mkdir(parents=True)
        self.results_dir.mkdir(parents=True)

    def get_stage_dir(self, stage_name: str) -> Path | None:
        path = self.stages_dir / stage_name
        return path if path.is_dir() else None

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        stage_path = request.contract.stage_path
        assert stage_path is not None
        path = self.stages_dir / stage_path
        return path if path.is_dir() else None

    def get_run_log_dir(self) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        return self.logs_dir

    def read_verified_node_artifact(
        self,
        request: NodeArtifactRequest,
        kind: str,
    ) -> VerifiedNodeArtifact:
        relative_path = (
            request.contract.output_path
            if kind == "output"
            else request.contract.findings_path
        )
        assert relative_path is not None
        path = self.results_dir / relative_path
        payload = path.read_bytes()
        return VerifiedNodeArtifact(
            path=path,
            payload=payload,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )


def make_static_content_ref(payload: bytes) -> str:
    return f"static-files/{hashlib.sha256(payload).hexdigest()}.txt"


def write_lineage_state_with_bundle(
    repo: Path,
    stage_dir: Path,
    source_commit: str,
    source_tree: str,
    result_commit: str,
    result_tree: str,
    bundle_name: str,
    extra_payload: dict[str, object] | None = None,
) -> str:
    overrides = extra_payload or {}
    node_id = str(overrides.get("node_id", "input"))
    task_id = str(overrides.get("task_id", "mock_executor_0"))
    round_num = int(overrides.get("round_num", 1))
    audit_round_num = overrides.get("audit_round_num")
    slug = invocation_slug(
        node_id,
        task_id,
        audit_round_num if isinstance(audit_round_num, int) else None,
        round_num,
    )
    result_ref = f"refs/crewplane/runs/demo-run/{node_id}/{slug}/result"
    bundle_dir = stage_dir / "workspace-bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / bundle_name
    run_fragment_git(repo, "update-ref", result_ref, result_commit)
    run_fragment_git(repo, "bundle", "create", bundle_path.as_posix(), result_ref)
    bundle_sha256 = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    state_path = stage_dir / "workspace-state.json"
    candidate_ref = f"{result_ref.removesuffix('/result')}/candidate"
    payload: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "run_id": "run",
        "run_key_name": "demo-run",
        "workflow_name": "demo",
        "workflow_signature": "0" * 64,
        "node_id": node_id,
        "task_id": task_id,
        "provider": "mock",
        "status": "succeeded",
        "role": "executor",
        "round_num": round_num,
        "audit_round_num": audit_round_num,
        "workspace_kind": "worktree",
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": WORKTREE_CONTRACT.model_dump(mode="json"),
        "git": {
            "object_format": run_fragment_git(
                repo, "rev-parse", "--show-object-format=storage"
            ),
            "repo_id": "repo",
            "run_base_commit": source_commit,
            "source_tree": source_tree,
            "git_top_level": repo.as_posix(),
            "active_git_dir": (repo / ".git").as_posix(),
            "common_git_dir": (repo / ".git").as_posix(),
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": source_commit,
            "tree": source_tree,
            "candidate_sequence": None,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "candidate_sequence": None,
        },
        "workspace": {
            "path": None,
            "effective_cwd": None,
            "materialization": "worktree_checkout",
            "writable": True,
            "lineage_producer": True,
            "retention": "retained",
            "retained_reason": None,
            "project_root_relative_path": ".",
            "reuse_generation": 1,
        },
        "execution": {
            "workspace_path": None,
            "checkout_root": None,
            "effective_cwd": None,
        },
        "process_drain": {"status": "confirmed"},
        "result": {
            "candidate_commit": result_commit,
            "result_commit": result_commit,
            "candidate_tree": result_tree,
            "result_tree": result_tree,
            "changed_path_count": 1,
        },
        "refs": {"candidate": candidate_ref, "result": result_ref},
        "bundle": {
            "path": bundle_path.relative_to(state_path.parent.parent).as_posix(),
            "sha256": bundle_sha256,
            "size_bytes": bundle_path.stat().st_size,
            "verified": True,
        },
        "ref_publication": {
            "phase": "published",
            "repository_id": "repo",
            "run_id": "run",
            "run_key_name": "demo-run",
            "node_id": node_id,
            "task_id": task_id,
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
    payload.update(overrides)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return result_ref


def make_fragment_plan(
    root: Path, content_ref: str | None = None
) -> PreflightExecutionPlan:
    static_content_ref = content_ref or make_static_content_ref(b"file")
    upstream = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(
            stage_path="input-stage",
            output_path="compiled-input.md",
            log_path="input-stage/logs",
            result_path="compiled-input.md",
        ),
        execution_policy=ExecutionPolicy(),
        input_content_ref="static-files/input.txt",
    )
    node = PreflightExecutionNode(
        id="build",
        mode="sequential",
        dependencies=["input"],
        render_plan_id="build",
        artifact_contract=ArtifactContract(
            stage_path="build-stage",
            output_path="build-result.md",
            log_path="build-stage/logs",
            result_path="build-result.md",
        ),
        execution_policy=ExecutionPolicy(),
        provider_records=[
            ProviderRecord(
                provider="mock",
                role=ProviderRole.EXECUTOR,
                task_id="mock_executor_0",
                agent_config_key="mock",
                invoker_alias="mock",
                agent_config_signature="agent-signature",
                invoker_config_signature="invoker-signature",
            )
        ],
    )
    return PreflightExecutionPlan(
        plan_schema_version=SCHEMA_VERSION,
        run_id="run",
        run_key_name="demo-run",
        project_root=root.as_posix(),
        context_root=root.as_posix(),
        manifest_root=(root / "manifests").as_posix(),
        created_at="2026-06-03T00:00:00",
        workflow_name="demo",
        workflow_signature="0" * 64,
        execution_order=["input", "build"],
        nodes=[upstream, node],
        render_plans=[
            RenderPlan(
                render_plan_id="build",
                node_id="build",
                streams=[
                    RenderStream(
                        target_role=ProviderRole.EXECUTOR,
                        fragments=[
                            Fragment(
                                fragment_index=0,
                                kind="literal",
                                source_role=PromptSegmentRole.SHARED,
                                text="A ",
                            ),
                            Fragment(
                                fragment_index=1,
                                kind="static_file_content",
                                source_role=PromptSegmentRole.SHARED,
                                content_ref=static_content_ref,
                            ),
                            Fragment(
                                fragment_index=2,
                                kind="literal",
                                source_role=PromptSegmentRole.SHARED,
                                text=" B ",
                            ),
                            Fragment(
                                fragment_index=3,
                                kind="runtime_locator_lookup",
                                source_role=PromptSegmentRole.SHARED,
                                locator={
                                    "node_id": "input",
                                    "artifact_name": "output",
                                },
                            ),
                            Fragment(
                                fragment_index=4,
                                kind="literal",
                                source_role=PromptSegmentRole.SHARED,
                                text=" C ",
                            ),
                            Fragment(
                                fragment_index=5,
                                kind="static_env",
                                source_role=PromptSegmentRole.SHARED,
                                key="API_TOKEN",
                                value_handle="env:API_TOKEN",
                            ),
                        ],
                    )
                ],
            )
        ],
        static_resources=[],
        token_catalog=[],
        dependency_graph=[
            DependencyEdge(
                source_node="input",
                target_node="build",
                artifact_name="output",
                dependency_signature=dependency_signature(
                    "input",
                    "build",
                    "output",
                ),
                target_locator="input.output",
                artifact_key="output",
            )
        ],
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        effective_runtime_config_signature="1" * 64,
        fingerprint_metadata={"payload_version": "1"},
    )


def run_fragment_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8").strip()
