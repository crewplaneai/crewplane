from __future__ import annotations

from pathlib import Path

from orchestrator_cli.cli.run.resume import build_resume_plan
from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight import PreflightCompilationPreview
from orchestrator_cli.core.preflight.source import PreflightWorkflowSource
from orchestrator_cli.core.workflow_models import WorkflowPlan
from orchestrator_cli.version import SCHEMA_VERSION
from tests.helpers.resume import WORKFLOW_NAME, WORKFLOW_SIGNATURE


def test_force_resume_plan_does_not_scan_unsafe_history(
    tmp_path,
    monkeypatch,
) -> None:
    orchestrator_dir = tmp_path / ".orchestrator"
    stages_root = orchestrator_dir / "execution-stages"
    original_lstat = Path.lstat

    def blocked_history_lstat(self):
        if self == stages_root:
            raise PermissionError("history blocked")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", blocked_history_lstat)

    resume_plan = build_resume_plan(
        Config(version=SCHEMA_VERSION, agents={}),
        _workflow_source(tmp_path),
        PreflightCompilationPreview(
            workflow_name=WORKFLOW_NAME,
            workflow_signature=WORKFLOW_SIGNATURE,
        ),
        tmp_path,
        orchestrator_dir,
        force=True,
    )

    assert resume_plan.decision.kind == "execute_full"


def _workflow_source(root: Path) -> PreflightWorkflowSource:
    workflow = WorkflowPlan(
        name=WORKFLOW_NAME,
        nodes=[],
    )
    return PreflightWorkflowSource.from_workflow(
        workflow,
        root_workflow_path=root / ".orchestrator" / "workflows" / "workflow.task.md",
    )
