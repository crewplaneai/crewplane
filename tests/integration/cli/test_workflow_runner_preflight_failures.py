from __future__ import annotations

import io
import json
import os
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from rich.console import Console

from crewplane.cli.workflow_runner import (
    write_early_preflight_failure_run,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.core.workflow.models import (
    WorkflowPlan,
)
from tests.helpers.working_directory import temporary_project_cwd
from tests.integration.cli.workflow_runner_support import (
    mock_runner_config,
    result_directories,
    run_directories,
    run_workflow,
    runner_workflow,
)


class WorkflowRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_failure_writes_failure_bundle(self) -> None:
        with temporary_project_cwd() as root:
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = runner_workflow("{{env:MISSING_REQUIRED_ENV}}")
            config = mock_runner_config()
            original_env = os.environ.pop("MISSING_REQUIRED_ENV", None)
            try:
                with pytest.raises(typer.Exit):
                    await run_workflow(workflow, config, console)
            finally:
                if original_env is not None:
                    os.environ["MISSING_REQUIRED_ENV"] = original_env

            run_dirs = run_directories(root)
            assert len(run_dirs) == 1
            preflight_dir = run_dirs[0] / "preflight"
            assert (preflight_dir / "diagnostics.json").exists()
            assert (preflight_dir / "metadata.json").exists()
            assert (preflight_dir / "manifest.json").exists()
            failure_manifest = json.loads(
                (preflight_dir / "manifest.json").read_text(encoding="utf-8")
            )
            assert failure_manifest["status"] == "preflight_failed"
            assert not (run_dirs[0] / "manifests").exists()
            assert result_directories(root) == []

    async def test_cli_availability_failure_writes_preflight_bundle(self) -> None:
        with temporary_project_cwd() as root:
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False)
            workflow = runner_workflow()
            config = mock_runner_config(invoker_implementation="cli")

            def missing_cli(command: str) -> str | None:
                assert command
                return None

            with pytest.raises(typer.Exit):
                await run_workflow(
                    workflow,
                    config,
                    console,
                    which_fn=missing_cli,
                )

            run_dirs = run_directories(root)
            assert len(run_dirs) == 1
            assert result_directories(root) == []
            diagnostics = json.loads(
                (run_dirs[0] / "preflight" / "diagnostics.json").read_text(
                    encoding="utf-8"
                )
            )
            assert diagnostics[0]["code"] == "PROVIDER-CLI"
            assert "not found in PATH" in diagnostics[0]["message"]
            assert (
                "Provider setup: docs/getting-started/provider-setup.md"
                in stream.getvalue()
            )
            failure_manifest = json.loads(
                (run_dirs[0] / "preflight" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            assert failure_manifest["status"] == "preflight_failed"

    async def test_preflight_warning_is_not_repeated_as_failure(self) -> None:
        with temporary_project_cwd():
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False)
            workflow = runner_workflow()
            config = mock_runner_config(invoker_implementation="cli")
            config.agents["alpha"] = AgentConfig(
                cli_cmd=["missing-provider"],
                provider_kind="codex",
                default_model="model-a",
                model_arg="--custom-model",
            )

            def missing_cli(command: str) -> str | None:
                assert command == "missing-provider"
                return None

            with pytest.raises(typer.Exit):
                await run_workflow(
                    workflow,
                    config,
                    console,
                    which_fn=missing_cli,
                )

            output = stream.getvalue()
            warning = "Agent 'alpha': remove model_arg"
            assert output.count(warning) == 1
            assert "Preflight PROVIDER-CONFIG" not in output
            assert "Preflight PROVIDER-CLI" in output

    async def test_runtime_config_snapshot_failure_writes_failure_bundle(self) -> None:
        with temporary_project_cwd() as root:
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = runner_workflow()
            config = mock_runner_config({"unknown_option": True})
            with pytest.raises(typer.Exit):
                await run_workflow(workflow, config, console)

            run_dirs = run_directories(root)
            assert len(run_dirs) == 1
            assert result_directories(root) == []
            diagnostics = json.loads(
                (run_dirs[0] / "preflight" / "diagnostics.json").read_text(
                    encoding="utf-8"
                )
            )
            assert diagnostics[0]["code"] == "RUNTIME-CONFIG"
            failure_manifest = json.loads(
                (run_dirs[0] / "preflight" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            assert failure_manifest["status"] == "preflight_failed"

    async def test_invoker_preflight_contract_failure_writes_failure_bundle(
        self,
    ) -> None:
        with temporary_project_cwd() as root:
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False)
            workflow = runner_workflow()
            config = mock_runner_config(invoker_implementation="cli")

            def failing_availability_errors(
                adapter: object,
                checked_workflow: WorkflowPlan,
                checked_config: Config,
                project_root: Path,
                executable_lookup: Callable[[str], str | None] | None = None,
            ) -> tuple[str, ...]:
                del (
                    adapter,
                    checked_workflow,
                    checked_config,
                    project_root,
                    executable_lookup,
                )
                raise RuntimeError("probe failed")

            with (
                patch(
                    "crewplane.adapters.invokers.cli.CliInvokerAdapter."
                    "collect_availability_errors",
                    new=failing_availability_errors,
                ),
                pytest.raises(typer.Exit),
            ):
                await run_workflow(workflow, config, console)

            run_dirs = run_directories(root)
            assert len(run_dirs) == 1
            assert result_directories(root) == []
            diagnostics = json.loads(
                (run_dirs[0] / "preflight" / "diagnostics.json").read_text(
                    encoding="utf-8"
                )
            )
            assert diagnostics[0]["code"] == "RUNTIME-CONFIG"
            assert "probe failed" in diagnostics[0]["message"]
            assert "Preflight RUNTIME-CONFIG" in stream.getvalue()
            failure_manifest = json.loads(
                (run_dirs[0] / "preflight" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            assert failure_manifest["status"] == "preflight_failed"

    def test_early_preflight_failure_uses_fallback_run_key(self) -> None:
        with temporary_project_cwd() as root:
            write_early_preflight_failure_run(
                root / "bad workflow.task.md",
                "frontmatter failed",
            )

            run_dirs = run_directories(root)
            assert len(run_dirs) == 1
            assert result_directories(root) == []
            assert run_dirs[0].name.startswith("bad-workflow-")
            metadata = json.loads(
                (run_dirs[0] / "preflight" / "metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            assert metadata["workflow_name"] is None
