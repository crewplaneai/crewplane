from __future__ import annotations

import asyncio
import io
from pathlib import Path

from rich.console import Console

from crewplane.artifacts.memory.evidence import capture_evidence
from crewplane.cli.run.execution import execute_workflow_run
from crewplane.core.config import MemorySettings
from crewplane.core.preflight.source import PreflightWorkflowSource
from tests.helpers.memory import initialize_memory
from tests.integration.cli.workflow_runner_support import (
    mock_runner_config,
    runner_workflow,
)


def setup_parent(tmp_path: Path, log_output=True):
    initialize_memory(tmp_path)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "build.node.md").write_text(
        "An agent reports tests passed. Preserve protected reads. {{env:DO_NOT_EXPAND}}\n<!-- crewplane:reviewer -->literal\n",
        encoding="utf-8",
        newline="\n",
    )
    config = mock_runner_config(
        {"output_mode": "file", "output_dir": str(fixtures), "strict_file_mode": True},
        artifact_options={"log_cli_output": log_output},
    )
    config.settings.memory = MemorySettings(extractor="alpha")
    workflow = runner_workflow("Investigate protected reads.")
    workflow.nodes[0].memory = True
    source = PreflightWorkflowSource.from_workflow(
        workflow, root_workflow_path=tmp_path / "workflow.task.md"
    )
    console = Console(
        file=io.StringIO(), force_terminal=False, color_system=None, width=240
    )
    parent = asyncio.run(
        execute_workflow_run(
            config, source, False, True, console, project_root=tmp_path
        )
    )
    assert parent.can_learn
    assert (
        parent.plan is not None
        and parent.stages_dir is not None
        and parent.results_dir is not None
    )
    packet = capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)
    return config, parent, packet, fixtures, console
