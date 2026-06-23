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
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.cli.run import execution as execution_module
from crewplane.cli.run import execution_helpers as execution_helpers_module
from crewplane.cli.run.context import WorkflowRunContext
from crewplane.cli.run.observability import WorkflowWarningRecorder
from crewplane.cli.run.resume import ResumePlan
from crewplane.core.config import Config
from crewplane.core.preflight import (
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
from tests.helpers.resume import WORKFLOW_IDENTITY, make_plan, make_run_manifest
from tests.helpers.workspace_branch_export import (
    branch_export_plan,
    history_record_for_output,
    write_node_manifest,
    write_result_bundle,
    write_workspace_state,
)
from tests.helpers.workspace_service import create_git_repo


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
        del args, kwargs

    async def noop_execute_workflow(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def fake_fulfill_branch_exports(
        plan_arg: object,
        output_arg: object,
    ) -> tuple[Path, ...]:
        assert plan_arg is plan
        assert output_arg is output
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

    def noop_finalize_run_manifest(*args: object, **kwargs: object) -> None:
        del args, kwargs

    refreshed_loggers = []
    printed_loggers = []

    def fake_refresh_successful_run_summary(logger: object) -> object:
        refreshed_loggers.append(logger)
        return "refreshed-summary"

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
        execution_module, "finalize_run_manifest", noop_finalize_run_manifest
    )
    monkeypatch.setattr(
        execution_module,
        "print_end_of_run_summary",
        record_print_end_of_run_summary,
    )
    monkeypatch.setattr(
        execution_module,
        "refresh_successful_run_summary",
        fake_refresh_successful_run_summary,
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
        ),
    )

    output_text = stream.getvalue()
    assert "Branch export fulfillment:" in output_text
    assert "worktree=primary" in output_text
    assert "operation=created" in output_text
    assert "branch=feature/exported" in output_text
    assert len(refreshed_loggers) == 1
    assert printed_loggers == ["refreshed-summary"]


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
    ) -> tuple[Path, ...]:
        assert source_arg is successful_run
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
        output.create_stage_dir("implement"),
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

    execution_helpers_module.fulfill_duplicate_skip_branch_exports(
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
