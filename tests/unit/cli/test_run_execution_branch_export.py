from __future__ import annotations

import asyncio
import io
import json
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.architecture.contracts import CanonicalIntegrationConfig
from crewplane.architecture.ports.runtime import RuntimeComponents
from crewplane.artifacts.manager import OutputManager
from crewplane.artifacts.resume.decision import ResumeDecision
from crewplane.artifacts.resume.validation import ValidatedResumeFrontier
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.cli.run import execution as execution_module
from crewplane.cli.run import execution_helpers as execution_helpers_module
from crewplane.cli.run.context import WorkflowRunContext
from crewplane.cli.run.observability import WorkflowWarningRecorder
from crewplane.cli.run.resume import ResumePlan
from crewplane.cli.run.terminalization import TerminalizationCoordinator
from crewplane.core.config import Config
from crewplane.core.preflight import (
    DependencyEdge,
    PreflightCompilationPreview,
    PreflightExecutionPlan,
)
from crewplane.core.preflight.runtime_config import (
    RuntimeConfigSnapshot,
    RuntimeConfigSnapshotOptions,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    make_node_state,
    make_plan,
    make_run_manifest,
)
from tests.helpers.workspace_branch_export import (
    branch_export_plan,
    history_record_for_output,
    write_node_manifest,
    write_result_bundle,
    write_workspace_state,
)
from tests.helpers.workspace_service import create_git_repo, run_git_text


def test_successful_run_prints_branch_export_fulfillment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=False,
        color_system=None,
        width=240,
    )
    plan = make_plan()
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    context = WorkflowRunContext(
        config=Config(version=SCHEMA_VERSION, agents={}),
        source=PreflightWorkflowSource.from_workflow(
            WorkflowPlan(name=plan.workflow_name, nodes=[]),
        ),
        console=console,
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
    )
    components = RuntimeComponents(
        artifact_store=output,
        base_invoker=object(),
        observers=(),
        suppress_progress_output=False,
    )

    async def noop_execute_workflow_with_observability(
        *args: object,
        **kwargs: object,
    ) -> None:
        del args
        on_scheduler_succeeded = kwargs["on_scheduler_succeeded"]
        assert callable(on_scheduler_succeeded)
        on_scheduler_succeeded()

    async def noop_execute_workflow(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def fake_fulfill_branch_exports(
        plan_arg: object,
        output_arg: object,
        resumed_node_ids: tuple[str, ...],
    ) -> tuple[Path, ...]:
        assert plan_arg is plan
        assert output_arg is output
        assert resumed_node_ids == ("implement",)
        record_path = output.stages_dir / "workspace-exports" / "primary.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {
                    "logical_worktree_name": "primary",
                    "status": "fulfilled",
                    "operation": "created",
                    "branch_name": "feature/exported",
                    "result_commit": "abcdef1234567890",
                }
            ),
            encoding="utf-8",
        )
        return (record_path,)

    printed_loggers = []

    def record_print_end_of_run_summary(console_arg: object, logger: object) -> None:
        del console_arg
        printed_loggers.append(logger)

    monkeypatch.setattr(
        execution_module,
        "execute_workflow_with_observability",
        noop_execute_workflow_with_observability,
    )
    monkeypatch.setattr(
        execution_module,
        "fulfill_branch_exports",
        fake_fulfill_branch_exports,
    )
    monkeypatch.setattr(
        execution_module,
        "print_end_of_run_summary",
        record_print_end_of_run_summary,
    )

    asyncio.run(
        execution_module.run_and_finalize_workflow(
            context=context,
            output=output,
            components=components,
            plan=plan,
            secret_context=SecretContext(),
            execute_workflow_impl=noop_execute_workflow,
            warning_recorder=WorkflowWarningRecorder(
                workflow=context.workflow,
                console=console,
            ),
            observability_hub_cls=None,
            workflow_identity=".crewplane/workflows/workflow.task.md",
            terminalization=TerminalizationCoordinator(
                output=output,
                workflow_name=plan.workflow_name,
            ),
            resumed_node_ids=("implement",),
        ),
    )

    output_text = stream.getvalue()
    assert "Branch export fulfillment:" in output_text
    assert "worktree=primary" in output_text
    assert "operation=created" in output_text
    assert "branch=feature/exported" in output_text
    assert len(printed_loggers) == 1


def test_duplicate_skip_prints_branch_export_fulfillment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=False,
        color_system=None,
        width=240,
    )
    plan = make_plan()
    preview = _preview_from_plan(plan)
    source = PreflightWorkflowSource.from_workflow(
        WorkflowPlan(name=plan.workflow_name, nodes=[]),
        root_workflow_path=tmp_path / ".crewplane" / "workflows" / "workflow.task.md",
    )
    manifest = make_run_manifest("success", "workflow--success", status="succeeded")
    successful_run = RunHistoryRecord(
        manifest=manifest,
        manifest_path=tmp_path
        / ".crewplane"
        / "execution-stages"
        / manifest.run_key_name
        / "manifests"
        / "run.json",
        run_dir=tmp_path / ".crewplane" / "execution-stages" / manifest.run_key_name,
        results_dir=tmp_path
        / ".crewplane"
        / "execution-results"
        / manifest.run_key_name,
    )

    def fake_compile_preview(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return preview

    def fake_build_resume_plan(*args: object, **kwargs: object) -> ResumePlan:
        del args, kwargs
        return ResumePlan(
            workflow_identity=WORKFLOW_IDENTITY,
            decision=ResumeDecision(kind="skip", successful_run=successful_run),
        )

    def fake_fulfill_branch_exports_from_history(
        plan_arg: object,
        source_arg: object,
        eligible_node_ids: object,
    ) -> tuple[Path, ...]:
        assert source_arg is successful_run
        assert eligible_node_ids is None
        assert isinstance(plan_arg, PreflightExecutionPlan)
        assert plan_arg.run_id == manifest.run_id
        record_path = tmp_path / "workspace-exports" / "primary.json"
        record_path.parent.mkdir(parents=True)
        record_path.write_text(
            json.dumps(
                {
                    "logical_worktree_name": "primary",
                    "status": "fulfilled",
                    "operation": "created",
                    "branch_name": "feature/exported",
                    "result_commit": "abcdef1234567890",
                }
            ),
            encoding="utf-8",
        )
        return (record_path,)

    monkeypatch.setattr(execution_module, "compile_preview", fake_compile_preview)
    monkeypatch.setattr(
        execution_module,
        "build_resume_plan",
        fake_build_resume_plan,
    )
    monkeypatch.setattr(
        execution_helpers_module,
        "fulfill_branch_exports_from_history",
        fake_fulfill_branch_exports_from_history,
    )

    asyncio.run(
        execution_module.execute_workflow_run(
            config=Config(version=SCHEMA_VERSION, agents={}),
            source=source,
            force=False,
            no_live=True,
            console=console,
            project_root=tmp_path,
            state_dir=tmp_path / ".crewplane",
        )
    )

    output_text = stream.getvalue()
    assert "Branch export fulfillment:" in output_text
    assert "worktree=primary" in output_text
    assert "operation=created" in output_text
    assert "branch=feature/exported" in output_text
    assert "Identical context detected" in output_text


def test_resume_reconciles_branch_export_history_before_output_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = Console(
        file=io.StringIO(),
        force_terminal=False,
        color_system=None,
        width=240,
    )
    plan = make_plan()
    preview = _preview_from_plan(plan)
    source = PreflightWorkflowSource.from_workflow(
        WorkflowPlan(name=plan.workflow_name, nodes=[]),
        root_workflow_path=tmp_path / ".crewplane" / "workflows" / "workflow.task.md",
    )
    manifest = make_run_manifest("source", "workflow--source", status="failed")
    source_run = RunHistoryRecord(
        manifest=manifest,
        manifest_path=tmp_path
        / ".crewplane"
        / "execution-stages"
        / manifest.run_key_name
        / "manifests"
        / "run.json",
        run_dir=tmp_path / ".crewplane" / "execution-stages" / manifest.run_key_name,
        results_dir=tmp_path
        / ".crewplane"
        / "execution-results"
        / manifest.run_key_name,
    )
    record_path = source_run.run_dir / "workspace-exports" / "primary.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        json.dumps(
            {
                "logical_worktree_name": "primary",
                "status": "prepared",
                "operation": "prepared",
                "branch_name": "feature/resumed",
            }
        ),
        encoding="utf-8",
    )

    def fake_compile_preview(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return preview

    def fake_build_resume_plan(*args: object, **kwargs: object) -> ResumePlan:
        del args, kwargs
        return ResumePlan(
            workflow_identity=WORKFLOW_IDENTITY,
            decision=ResumeDecision(kind="resume", resume_source=source_run),
            frontier=ValidatedResumeFrontier(
                source=source_run,
                node_states={"a": make_node_state(manifest, "a", [])},
            ),
        )

    def reconcile_branch_exports(
        plan_arg: PreflightExecutionPlan,
        source_arg: RunHistoryRecord,
        eligible_node_ids: object,
    ) -> tuple[Path, ...]:
        assert plan_arg.run_id == manifest.run_id
        assert source_arg is source_run
        assert eligible_node_ids == ("a",)
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        assert payload["status"] == "prepared"
        payload.update({"status": "fulfilled", "operation": "verified_existing"})
        record_path.write_text(json.dumps(payload), encoding="utf-8")
        return (record_path,)

    def skip_historical_summary_refresh(
        plan_arg: PreflightExecutionPlan,
        source_arg: RunHistoryRecord,
    ) -> None:
        assert plan_arg.run_id == manifest.run_id
        assert source_arg is source_run

    class OutputAllocationReached(RuntimeError):
        pass

    def stop_after_reconciliation(
        context_arg: object,
        snapshot_result_arg: object,
        warning_recorder_arg: object,
    ) -> object:
        del context_arg, snapshot_result_arg, warning_recorder_arg
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        assert payload["status"] == "fulfilled"
        raise OutputAllocationReached

    monkeypatch.setattr(execution_module, "compile_preview", fake_compile_preview)
    monkeypatch.setattr(
        execution_module,
        "build_resume_plan",
        fake_build_resume_plan,
    )
    monkeypatch.setattr(
        execution_helpers_module,
        "fulfill_branch_exports_from_history",
        reconcile_branch_exports,
    )
    monkeypatch.setattr(
        execution_helpers_module,
        "refresh_historical_run_summary",
        skip_historical_summary_refresh,
    )
    monkeypatch.setattr(
        execution_module,
        "allocate_run_output",
        stop_after_reconciliation,
    )

    with pytest.raises(OutputAllocationReached):
        asyncio.run(
            execution_module.execute_workflow_run(
                config=Config(version=SCHEMA_VERSION, agents={}),
                source=source,
                force=False,
                no_live=True,
                console=console,
                project_root=tmp_path,
                state_dir=tmp_path / ".crewplane",
            )
        )


def test_resume_historical_branch_export_skips_incomplete_final_checkpoint(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = _two_node_same_worktree_plan(
        branch_export_plan(repo, tmp_path, branch_name="feature/resume")
    )
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "first checkpoint\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    history = history_record_for_output(output)
    source_run = RunHistoryRecord(
        manifest=history.manifest.model_copy(update={"status": "failed"}),
        manifest_path=history.manifest_path,
        run_dir=history.run_dir,
        results_dir=history.results_dir,
    )
    context = WorkflowRunContext(
        config=Config(version=SCHEMA_VERSION, agents={}),
        source=PreflightWorkflowSource.from_workflow(
            WorkflowPlan(name=plan.workflow_name, nodes=[]),
        ),
        console=Console(file=io.StringIO(), force_terminal=False, color_system=None),
        project_root=repo,
        state_dir=tmp_path / ".crewplane",
    )

    execution_helpers_module.fulfill_historical_branch_exports(
        context,
        _preview_from_plan(plan),
        source_run,
        ("implement",),
    )

    assert not (source_run.run_dir / "workspace-exports/primary.json").exists()
    assert not run_git_text(repo, "branch", "--list", "feature/resume")


def test_duplicate_skip_refreshes_historical_summary_after_branch_export(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=False,
        color_system=None,
        width=240,
    )
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(
        repo,
        tmp_path,
        branch_name=None,
        create_branch=False,
    )
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    write_node_manifest(output, plan)
    history = history_record_for_output(output)
    summary_path = history.run_dir / "logs" / "summary.md"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text("stale summary without branch export\n", encoding="utf-8")
    context = WorkflowRunContext(
        config=Config(version=SCHEMA_VERSION, agents={}),
        source=PreflightWorkflowSource.from_workflow(
            WorkflowPlan(name=plan.workflow_name, nodes=[]),
        ),
        console=console,
        project_root=repo,
        state_dir=tmp_path / ".crewplane",
    )

    execution_helpers_module.fulfill_historical_branch_exports(
        context,
        _preview_from_plan(plan),
        history,
    )

    summary_text = summary_path.read_text(encoding="utf-8")
    assert "stale summary without branch export" not in summary_text
    assert "branch_export=status=skipped" in summary_text
    assert "operation=skipped" in summary_text


def _preview_from_plan(plan: PreflightExecutionPlan) -> PreflightCompilationPreview:
    return PreflightCompilationPreview(
        workflow_name=plan.workflow_name,
        workflow_signature=plan.workflow_signature,
        execution_order=list(plan.execution_order),
        nodes=list(plan.nodes),
        render_plans=list(plan.render_plans),
        static_resources=list(plan.static_resources),
        workspace_file_locators=list(plan.workspace_file_locators),
        token_catalog=list(plan.token_catalog),
        dependency_graph=list(plan.dependency_graph),
        runtime_config_snapshot=_runtime_snapshot(),
        effective_runtime_config_signature=plan.effective_runtime_config_signature,
        workspace_source=plan.workspace_source,
        fingerprint_metadata=dict(plan.fingerprint_metadata),
    )


def _two_node_same_worktree_plan(
    plan: PreflightExecutionPlan,
) -> PreflightExecutionPlan:
    first = plan.nodes[0]
    policy = first.workspace_policy
    assert policy is not None
    final = first.model_copy(
        update={
            "id": "final",
            "dependencies": [first.id],
            "render_plan_id": "final",
            "workspace_policy": policy.model_copy(
                update={"source_kind": "node", "source_node_id": first.id}
            ),
            "artifact_contract": first.artifact_contract.model_copy(
                update={
                    "stage_path": "final",
                    "output_path": "final/output.md",
                    "result_path": "final/output.md",
                    "log_path": "final/logs",
                }
            ),
        }
    )
    final_render_plan = plan.render_plans[0].model_copy(
        update={"render_plan_id": "final", "node_id": "final"}
    )
    dependency = DependencyEdge(
        source_node=first.id,
        target_node=final.id,
        artifact_name="output",
        dependency_signature="c" * 64,
        target_locator=f"{first.id}.output",
        artifact_key="output",
    )
    return plan.model_copy(
        update={
            "execution_order": [first.id, final.id],
            "nodes": [first, final],
            "render_plans": [plan.render_plans[0], final_render_plan],
            "dependency_graph": [dependency],
        }
    )


def _runtime_snapshot() -> RuntimeConfigSnapshot:
    integration = CanonicalIntegrationConfig(
        implementation="filesystem",
        resolved_identity="filesystem",
    )
    return RuntimeConfigSnapshot.build(
        config=Config(version=SCHEMA_VERSION, agents={}),
        invoker=integration,
        artifacts=integration,
        ui=integration,
        options=RuntimeConfigSnapshotOptions(no_live=True),
    )
