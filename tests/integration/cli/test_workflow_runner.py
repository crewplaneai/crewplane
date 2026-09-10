from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import typer
from rich.console import Console

from crewplane.adapters.artifacts.terminal_history import (
    FilesystemTerminalHistoryReader,
)
from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
)
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.manager import OutputManager
from crewplane.cli.run.workspace.git_source import (
    GIT_MIN_VERSION,
    parse_git_version,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.core.preflight import (
    signature_for_payload,
)
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
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

DESCRIPTOR_LEAK_TOKENS = (
    "log_presentation_format",
    "log_presentation_profile",
    "json_lines",
)


class DuplicateReportingArtifactsAdapter:
    create_store_calls = 0

    @classmethod
    def reset(cls) -> None:
        cls.create_store_calls = 0

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options=dict(options or {}),
            option_scopes={key: "artifact" for key in dict(options or {})},
        )

    def create_store(
        self,
        workflow_name: str,
        state_dir: Path,
        project_root: Path,
        options: Mapping[str, Any] | None = None,
    ) -> ArtifactStorePort:
        type(self).create_store_calls += 1
        resolved_options = dict(options or {})
        return OutputManager(
            workflow_name,
            base_dir=state_dir,
            template_base_dir=project_root,
            log_cli_output=bool(resolved_options.get("log_cli_output", False)),
        )

    def create_terminal_history_reader(
        self,
        state_dir: Path,
        options: Mapping[str, Any] | None = None,
    ) -> FilesystemTerminalHistoryReader:
        del options
        return FilesystemTerminalHistoryReader(state_dir.resolve())


class PreflightOrderingInvoker:
    def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by invoker protocol.
        return None

    async def invoke(  # type: ignore[no-untyped-def]
        self,
        config,  # noqa: ARG002 - Required by invoker protocol.
        model,  # noqa: ARG002 - Required by invoker protocol.
        prompt,  # noqa: ARG002 - Required by invoker protocol.
        output_file,
        cwd,  # noqa: ARG002 - Required by invoker protocol.
        log_file=None,  # noqa: ARG002 - Required by invoker protocol.
        invocation_context=None,  # noqa: ARG002 - Required by invoker protocol.
    ) -> None:
        output_file.write_text("ok", encoding="utf-8")


class PreflightOrderingInvokerAdapter:
    preflight_plan_exists_at_create = False

    @classmethod
    def reset(cls) -> None:
        cls.preflight_plan_exists_at_create = False

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        if options:
            raise ValueError(f"Unsupported options: {sorted(options)}")
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options={},
            option_scopes={},
        )

    def create_invoker(
        self,
        config: Config,  # noqa: ARG002 - Required by adapter protocol.
        options: Mapping[str, Any] | None = None,  # noqa: ARG002 - Required by adapter protocol.
    ) -> PreflightOrderingInvoker:
        type(self).preflight_plan_exists_at_create = any(
            Path(".crewplane").glob("execution-stages/*/preflight/execution-plan.json")
        )
        return PreflightOrderingInvoker()


def _input_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="InputTask",
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/input.md}}",
            )
        ],
    )


def _descriptor_leakage_paths(
    run_dir: Path,
    result_dir: Path,
    preflight_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    paths.extend(_text_files_under(preflight_dir))
    paths.extend(_text_files_under(run_dir / "manifests"))
    paths.extend(_text_files_under(result_dir))
    paths.extend(sorted(path for path in run_dir.rglob("*.log") if path.is_file()))
    summary_path = run_dir / "logs" / "summary.md"
    if summary_path.exists():
        paths.append(summary_path)
    return sorted(set(paths))


def _text_files_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", root.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8").strip()


def _local_git_supports_workspace_policy() -> bool:
    try:
        version_text = subprocess.run(
            ["git", "--version"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    version = parse_git_version(version_text)
    return version is not None and version >= GIT_MIN_VERSION


def _assert_descriptor_metadata_event_persisted(
    run_dir: Path,
) -> None:
    event_log_path = run_dir / "logs" / "events.ndjson"
    records = [
        json.loads(line)
        for line in event_log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        record.get("log_presentation_format") == "json_lines"
        and record.get("log_presentation_profile") == "mock"
        for record in records
    )


def _assert_descriptor_metadata_absent(
    paths: list[Path],
) -> None:
    assert len(paths) > 0
    for path in paths:
        content = path.read_text(encoding="utf-8")
        for token in DESCRIPTOR_LEAK_TOKENS:
            assert token not in content, f"{token} leaked into {path}"


class WorkflowRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_signature_skips_without_run_allocation(self) -> None:
        with temporary_project_cwd() as root:
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False, color_system=None)
            workflow = runner_workflow()
            config = mock_runner_config()
            await run_workflow(workflow, config, console)
            run_count = len(run_directories(root))
            await run_workflow(workflow, config, console)

            assert len(run_directories(root)) == run_count
            assert "Identical context detected" in stream.getvalue()

    async def test_non_filesystem_artifact_real_run_fails_before_run_allocation(
        self,
    ) -> None:
        with temporary_project_cwd() as root:
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False, color_system=None)
            workflow = runner_workflow()
            config = mock_runner_config(
                artifact_implementation=(
                    f"{__name__}:DuplicateReportingArtifactsAdapter"
                ),
                artifact_options={"marker": "duplicate"},
            )
            DuplicateReportingArtifactsAdapter.reset()
            with pytest.raises(
                RuntimeError,
                match="Real execution requires the built-in filesystem artifacts backend",
            ):
                await run_workflow(workflow, config, console)

            assert DuplicateReportingArtifactsAdapter.create_store_calls == 0
            assert run_directories(root) == []
            assert result_directories(root) == []
            assert not (root / ".crewplane" / "locks").exists()

    async def test_reasoning_validation_stops_real_run_before_execution_setup(
        self,
    ) -> None:
        with temporary_project_cwd():
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False, color_system=None)
            workflow = runner_workflow()
            workflow.nodes[0] = workflow.nodes[0].model_copy(
                update={"providers": [ProviderSpec(provider="alpha", reasoning="high")]}
            )
            execute_workflow_mock = AsyncMock()
            with (
                patch(
                    "crewplane.cli.run.execution.acquire_same_context_lock",
                    side_effect=AssertionError("reasoning validation reached locking"),
                ) as acquire_lock,
                patch(
                    "crewplane.cli.run.execution.allocate_run_output",
                    side_effect=AssertionError(
                        "reasoning validation reached allocation"
                    ),
                ) as allocate_output,
                pytest.raises(typer.Exit) as raised,
            ):
                await run_workflow(
                    workflow,
                    mock_runner_config(),
                    console,
                    execute_workflow_impl=execute_workflow_mock,
                )

            assert raised.value.exit_code == 1
            assert (
                "first-class reasoning requires the built-in CLI invoker"
                in stream.getvalue()
            )
            acquire_lock.assert_not_called()
            allocate_output.assert_not_called()
            execute_workflow_mock.assert_not_called()

    async def test_force_ignores_duplicate_signature(self) -> None:
        with temporary_project_cwd() as root:
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = runner_workflow()
            config = mock_runner_config()
            await run_workflow(workflow, config, console)
            await run_workflow(workflow, config, console, force=True)

            assert len(run_directories(root)) == 2

    async def test_successful_run_writes_preflight_bundle_and_redacted_manifest(
        self,
    ) -> None:
        with temporary_project_cwd() as root:
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = runner_workflow("{{env:API_TOKEN}}")
            config = mock_runner_config()
            config.agents["alpha"].extra_args = ["--api-key", "super-secret"]
            raw_agent_config_signature = signature_for_payload(
                config.agents["alpha"].model_dump(mode="json", exclude_none=True)
            )
            config_yaml_content = "api_token: super-secret\n"
            raw_config_yaml_signature = hashlib.sha256(
                config_yaml_content.encode("utf-8")
            ).hexdigest()
            original_api_token = os.environ.get("API_TOKEN")
            os.environ["API_TOKEN"] = "env-secret"
            try:
                await run_workflow(workflow, config, console)
            finally:
                if original_api_token is None:
                    os.environ.pop("API_TOKEN", None)
                else:
                    os.environ["API_TOKEN"] = original_api_token

            run_dirs = run_directories(root)
            result_dirs = result_directories(root)
            assert len(run_dirs) == 1
            assert len(result_dirs) == 1
            preflight_dir = run_dirs[0] / "preflight"
            expected_preflight_files = {
                "dependency-graph.json",
                "execution-bundle.json",
                "execution-plan.json",
                "manifest.json",
                "metadata.json",
                "render-plans.json",
                "runtime-config-snapshot.json",
                "static-resources.json",
                "summary.md",
                "token-catalog.json",
            }
            assert expected_preflight_files.issubset(
                {path.name for path in preflight_dir.iterdir()}
            )
            preflight_manifest = json.loads(
                (preflight_dir / "manifest.json").read_text(encoding="utf-8")
            )
            assert preflight_manifest["status"] == "preflight_succeeded"
            plan_text = (preflight_dir / "execution-plan.json").read_text(
                encoding="utf-8"
            )
            assert raw_agent_config_signature not in plan_text
            assert "env-secret" not in plan_text
            plan = json.loads(plan_text)
            assert plan["runtime_config_snapshot"]["sensitive_config_paths"] == [
                "agents.alpha.extra_args.1"
            ]
            assert len(plan["value_fingerprints"]) >= 1
            assert all("value" not in record for record in plan["value_fingerprints"])
            assert plan["value_fingerprints"][0]["key"] == "API_TOKEN"
            assert len(plan["value_fingerprints"][0]["fingerprint"]) == 64
            execution_bundle = json.loads(
                (preflight_dir / "execution-bundle.json").read_text(encoding="utf-8")
            )
            assert execution_bundle["value_fingerprints"] == plan["value_fingerprints"]
            manifest_path = run_dirs[0] / "manifests" / "run.json"
            manifest_text = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)

            assert "config_yaml" not in manifest
            assert "config_yaml_sha256" not in manifest
            assert "super-secret" not in manifest_text
            assert "env-secret" not in manifest_text
            assert raw_config_yaml_signature not in manifest_text
            assert raw_agent_config_signature not in manifest_text
            assert (
                manifest["runtime_config_snapshot"] == plan["runtime_config_snapshot"]
            )
            _assert_descriptor_metadata_event_persisted(run_dirs[0])
            _assert_descriptor_metadata_absent(
                _descriptor_leakage_paths(
                    run_dir=run_dirs[0],
                    result_dir=result_dirs[0],
                    preflight_dir=preflight_dir,
                ),
            )

    async def test_workspace_enabled_input_only_run_skips_managed_workspace(
        self,
    ) -> None:
        if not _local_git_supports_workspace_policy():
            self.skipTest("Git 2.34.1+ is required for workspace source policy")
        with temporary_project_cwd() as root:
            cache_root = root.parent / f"{root.name}-workspace-cache"
            (root / "docs").mkdir()
            (root / "docs" / "input.md").write_text(
                "workspace requirements\n",
                encoding="utf-8",
            )
            _git(root, "init")
            _git(root, "config", "user.name", "Crewplane Test")
            _git(root, "config", "user.email", "crewplane-test@example.invalid")
            _git(root, "add", "docs/input.md")
            _git(root, "commit", "-m", "initial")
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = _input_workflow()
            config = mock_runner_config()
            config.settings.workspace.enabled = True
            config.settings.workspace.cache_root = cache_root.as_posix()
            await run_workflow(workflow, config, console)

            run_dirs = run_directories(root)
            result_dirs = result_directories(root)
            assert len(run_dirs) == 1
            assert len(result_dirs) == 1
            workspace_state_path = run_dirs[0] / "requirements" / "workspace-state.json"
            assert not workspace_state_path.exists()
            assert not cache_root.exists()

    async def test_runtime_receives_preflight_plan_agent_configs_only(self) -> None:
        with temporary_project_cwd():
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = runner_workflow()
            config = mock_runner_config()
            config.agents["alpha"] = AgentConfig(
                cli_cmd=["preflight-command"],
                default_model="model-a",
            )
            captured_command: list[str] = []

            async def fake_execute_workflow(
                plan,
                output,  # noqa: ARG001 - Required by callback signature.
                invoker,  # noqa: ARG001 - Required by callback signature.
                secret_context,  # noqa: ARG001 - Required by callback signature.
                event_sink=None,  # noqa: ARG001 - Required by callback signature.
                run_id=None,  # noqa: ARG001 - Required by callback signature.
                suppress_progress_output=False,  # noqa: ARG001 - Required by callback signature.
                workflow_identity=None,  # noqa: ARG001 - Required by callback signature.
                resumed_node_ids=(),  # noqa: ARG001 - Required by callback signature.
            ) -> None:
                captured_command.extend(
                    plan.runtime_config_snapshot["agents"]["alpha"]["cli_cmd"]
                )

            await run_workflow(
                workflow,
                config,
                console,
                execute_workflow_impl=fake_execute_workflow,
            )

            assert captured_command == ["preflight-command"]

    async def test_preflight_plan_is_materialized_before_invoker_construction(
        self,
    ) -> None:
        with temporary_project_cwd():
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = runner_workflow()
            config = mock_runner_config(
                invoker_implementation=(f"{__name__}:PreflightOrderingInvokerAdapter"),
                options={},
            )
            PreflightOrderingInvokerAdapter.reset()
            await run_workflow(workflow, config, console)

            assert PreflightOrderingInvokerAdapter.preflight_plan_exists_at_create
