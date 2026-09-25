from pathlib import Path

import pytest
from rich.console import Console

from crewplane.cli.run.context import build_workflow_run_context
from crewplane.core.config import Config
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.version import SCHEMA_VERSION


@pytest.mark.parametrize("root_kind", ["omitted", "cwd", "relative", "absolute"])
@pytest.mark.parametrize("state_kind", ["omitted", "relative", "absolute"])
def test_run_context_preserves_identity_and_resolves_paths_without_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_kind: str, state_kind: str
) -> None:
    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)
    project_root = {
        "omitted": None,
        "cwd": Path("."),
        "relative": Path("../project"),
        "absolute": tmp_path / "project",
    }[root_kind]
    state_dir = {
        "omitted": None,
        "relative": Path("relative-state"),
        "absolute": tmp_path / "absolute-state",
    }[state_kind]
    config = Config(version=SCHEMA_VERSION, agents={})
    workflow = WorkflowPlan(name="workflow", nodes=[])
    source = PreflightWorkflowSource.from_workflow(workflow)
    console = Console()

    context = build_workflow_run_context(
        config, source, console, project_root, state_dir
    )

    expected_root = (
        working_dir if root_kind in {"omitted", "cwd"} else tmp_path / "project"
    )
    expected_state = {
        "omitted": expected_root / ".crewplane",
        "relative": working_dir / "relative-state",
        "absolute": tmp_path / "absolute-state",
    }[state_kind]
    assert context.config is config
    assert context.source is source
    assert context.workflow is workflow
    assert context.console is console
    assert context.project_root == expected_root
    assert context.state_dir == expected_state
    assert list(tmp_path.iterdir()) == [working_dir]
    assert list(working_dir.iterdir()) == []
