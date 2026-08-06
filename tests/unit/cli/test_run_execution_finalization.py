from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.architecture.ports.runtime import RuntimeComponents
from crewplane.artifacts.manager import OutputManager
from crewplane.cli.run import execution as execution_module
from crewplane.cli.run.context import WorkflowRunContext
from crewplane.cli.run.observability import WorkflowWarningRecorder
from crewplane.core.config import Config
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_plan


def test_branch_export_failure_preserves_failed_run_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
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
    warning_recorder = WorkflowWarningRecorder(
        workflow=context.workflow,
        console=console,
    )
    export_error = RuntimeError("branch export failed")
    finalizations: list[tuple[object, ...]] = []
    refreshed_errors: list[Exception] = []
    printed_summaries: list[object] = []

    async def complete_execution(
        *args: object,  # noqa: ARG001 - Required by execution test double.
        **kwargs: object,  # noqa: ARG001 - Required by execution test double.
    ) -> None:
        return None

    def fail_branch_export(
        plan_arg: object,  # noqa: ARG001 - Required by branch-export test double.
        output_arg: object,  # noqa: ARG001 - Required by branch-export test double.
    ) -> tuple[Path, ...]:
        raise export_error

    def record_finalization(
        output_arg: object,
        status: object,
        failure_message: object = None,
        cancel_reason: object = None,
    ) -> None:
        finalizations.append((output_arg, status, failure_message, cancel_reason))

    def refresh_failed_summary(
        logger: object,
        workflow: object,
        run_id: object,
        exc: Exception,
    ) -> object:
        assert logger is warning_recorder.persistent_logger
        assert workflow is context.workflow
        assert run_id is warning_recorder.run_id
        refreshed_errors.append(exc)
        return "failed-summary"

    def record_summary(console_arg: object, summary: object) -> None:
        assert console_arg is console
        printed_summaries.append(summary)

    monkeypatch.setattr(
        execution_module,
        "execute_workflow_with_observability",
        complete_execution,
    )
    monkeypatch.setattr(
        execution_module,
        "fulfill_branch_exports",
        fail_branch_export,
    )
    monkeypatch.setattr(
        execution_module,
        "finalize_run_manifest",
        record_finalization,
    )
    monkeypatch.setattr(
        execution_module,
        "refresh_failed_run_summary",
        refresh_failed_summary,
    )
    monkeypatch.setattr(
        execution_module,
        "print_end_of_run_summary",
        record_summary,
    )

    with pytest.raises(RuntimeError, match="branch export failed") as raised:
        asyncio.run(
            execution_module.run_and_finalize_workflow(
                context=context,
                output=output,
                components=components,
                plan=plan,
                secret_context=SecretContext(),
                execute_workflow_impl=complete_execution,
                warning_recorder=warning_recorder,
                observability_hub_cls=None,
                workflow_identity=".crewplane/workflows/workflow.task.md",
            )
        )

    assert raised.value is export_error
    assert finalizations == [(output, "failed", "branch export failed", None)]
    assert refreshed_errors == [export_error]
    assert printed_summaries == ["failed-summary"]
