from __future__ import annotations

from pathlib import Path
from shutil import copytree
from typing import Literal

from crewplane.architecture.contracts.artifacts import build_task_round_filename
from crewplane.artifacts import OutputManager
from crewplane.artifacts.naming import run_manifest_relative_path
from crewplane.artifacts.resume.checkpoint_hydration import hydrate_review_checkpoints
from crewplane.artifacts.resume.validation import (
    ValidatedResumeFrontier,
    validate_resume_frontier,
)
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.core.review_checkpoint_state import CheckpointProjectObservation
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.runtime.execution.review_loop.candidate_identity import (
    project_fingerprint,
)
from crewplane.runtime.execution.review_loop.checkpoint import (
    restore_selected_checkpoints,
)
from crewplane.runtime.execution.runtime_context import CompiledRuntimeContext
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_plan, make_run_manifest
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    compile_test_plan,
)


def checkpoint_run(
    root: Path,
    consensus_on_exhaustion: Literal["fatal", "continue"] = "fatal",
    **node_fields: object,
) -> tuple[OutputManager, CompiledRuntimeContext]:
    root.mkdir(parents=True, exist_ok=True)
    output = OutputManager("Checkpoint", base_dir=root)
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            name: AgentConfig(cli_cmd=["mock"], default_model=name)
            for name in ("exec", "review")
        },
        settings=Settings(
            max_concurrent_nodes=1,
            sequential_consensus_on_exhaustion=consensus_on_exhaustion,
        ),
    )
    prompt = "Implement and review the task."
    if node_fields.get("review_starts_with") == "reviewer":
        (root / "context.md").write_text("Existing implementation to review.")
        prompt += " {{file:context.md}}"
    node = WorkflowNode.model_validate(
        {
            "id": "review",
            "mode": "sequential",
            "prompt_segments": [{"role": "shared", "content": prompt}],
            "depth": 2,
            "audit_rounds": 3,
            "providers": [
                {"provider": "exec", "role": "executor"},
                {"provider": "review", "role": "reviewer"},
            ],
            **node_fields,
        }
    )
    _, runtime = compile_test_plan(
        config, WorkflowPlan(name=output.task_name, nodes=[node]), output
    )
    runtime.workflow_identity = "checkpoint.task.md"
    write_manifest(output, runtime)
    return output, runtime


def write_manifest(
    output: OutputManager, runtime: CompiledRuntimeContext, status: str = "running"
) -> RunHistoryRecord:
    manifest = make_run_manifest(
        output.run_id,
        output.run_key_name,
        status=status,
        workflow_identity=runtime.workflow_identity or runtime.plan.workflow_name,
        workflow_name=runtime.plan.workflow_name,
        workflow_signature=runtime.plan.workflow_signature,
    )
    summaries = output.read_hydrated_review_checkpoints()
    if summaries:
        origin = summaries[0].resume_origin
        manifest = manifest.model_copy(
            update={
                "resumed_review_checkpoints": summaries,
                "resume_source_run_id": origin.source_run_id,
                "resume_source_run_key_name": origin.source_run_key_name,
            }
        )
    output.write_run_manifest(manifest)
    return RunHistoryRecord(
        manifest,
        output.stages_dir / run_manifest_relative_path(),
        output.stages_dir,
        output.results_dir,
    )


def observation(
    runtime: CompiledRuntimeContext, output: OutputManager
) -> CheckpointProjectObservation:
    fingerprint = project_fingerprint(
        Path(runtime.plan.project_root), (output.stages_dir, output.results_dir)
    )
    return CheckpointProjectObservation(
        fingerprint=fingerprint, reliable=fingerprint is not None
    )


def hydrate_checkpoint(
    output: OutputManager, runtime: CompiledRuntimeContext
) -> tuple[OutputManager, CompiledRuntimeContext]:
    frontier, fresh, restored = prepare_checkpoint_hydration(output, runtime)
    hydrate_review_checkpoints(frontier, restored.plan, fresh)
    restore_selected_checkpoints(restored, fresh)
    return fresh, restored


def prepare_checkpoint_hydration(
    output: OutputManager, runtime: CompiledRuntimeContext
) -> tuple[ValidatedResumeFrontier, OutputManager, CompiledRuntimeContext]:
    source = write_manifest(output, runtime, "failed")
    frontier = validate_resume_frontier(
        source, runtime.plan, observation(runtime, output)
    )
    assert frontier.checkpoint_node_ids == ("review",)
    fresh = OutputManager(output.task_name, base_dir=output.base_dir)
    plan = runtime.plan.model_copy(
        update={
            "run_id": fresh.run_id,
            "run_key_name": fresh.run_key_name,
            "context_root": fresh.stages_dir.as_posix(),
            "manifest_root": (fresh.stages_dir / "manifests").as_posix(),
        }
    )
    restored = CompiledRuntimeContext(
        plan, runtime.secret_context, workflow_identity=runtime.workflow_identity
    )
    static = output.stages_dir / "preflight" / "static-files"
    if static.is_dir():
        copytree(
            static, fresh.stages_dir / "preflight" / "static-files", dirs_exist_ok=True
        )
    write_manifest(fresh, restored)
    return frontier, fresh, restored


def open_checkpoint(output: OutputManager) -> OpenReviewCheckpoint:
    checkpoint = output.read_review_checkpoint("review")
    assert isinstance(checkpoint, OpenReviewCheckpoint)
    return checkpoint


def checkpoint_payload() -> dict[str, object]:
    plan = make_plan(review_loop=True)
    node = plan.nodes[0]
    task = node.provider_records[0]
    path = f"a/{build_task_round_filename(task.task_id, 1)}"
    candidate = {
        "task_id": task.task_id,
        "role": "executor",
        "audit": 1,
        "local_round": 1,
        "producer_audit": 1,
        "producer_round": 1,
        "output_path": path,
        "identity": {
            "kind": "unverified",
            "fingerprint": None,
            "reason": "capture unavailable",
        },
    }
    return {
        "kind": "open",
        "run_state_schema_version": 1,
        "plan_schema_version": plan.plan_schema_version,
        "workflow_identity": "workflow.task.md",
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "run_id": "source",
        "run_key_name": "workflow--source",
        "node_id": "a",
        "tasks": [
            {"task_id": p.task_id, "role": p.role} for p in node.provider_records
        ],
        "audit": 1,
        "local_round": 1,
        "next_phase": "reviewers",
        "progress": {
            "executed_audit_rounds": 1,
            "active_audit": {
                "executor_outputs": [candidate],
                "latest_valid_executor_outputs": [candidate],
            },
        },
        "files": [
            {
                "task_id": task.task_id,
                "role": "executor",
                "audit": 1,
                "local_round": 1,
                "purpose": "executor_output",
                "relative_path": path,
                "signature": (9, "a" * 64),
            }
        ],
        "project_observation": {"fingerprint": "b" * 64, "reliable": True},
    }
