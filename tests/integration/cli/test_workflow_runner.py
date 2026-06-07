from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from orchestrator_cli.architecture.api_version import EXT_API_VERSION
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.artifacts.manager import OutputManager
from orchestrator_cli.cli.workflow_runner import (
    execute_workflow_run,
    write_early_preflight_failure_run,
)
from orchestrator_cli.core.config import AgentConfig, Config, Settings
from orchestrator_cli.core.preflight import (
    PreflightWorkflowSource,
    signature_for_payload,
)
from orchestrator_cli.core.preflight.runtime_config import CanonicalIntegrationConfig
from orchestrator_cli.core.versions import CONFIG_SCHEMA_VERSION
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)


class DuplicateReportingArtifactsAdapter:
    create_store_calls = 0
    workflow_signature_exists_calls = 0
    last_lookup: tuple[str, Path, dict[str, Any], str] | None = None

    @classmethod
    def reset(cls) -> None:
        cls.create_store_calls = 0
        cls.workflow_signature_exists_calls = 0
        cls.last_lookup = None

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            api_version=EXT_API_VERSION,
            options=dict(options or {}),
            option_scopes={key: "artifact" for key in dict(options or {})},
        )

    def workflow_signature_exists(
        self,
        workflow_name: str,
        orchestrator_dir: Path,
        options: Mapping[str, Any] | None,
        workflow_signature: str,
    ) -> bool:
        type(self).workflow_signature_exists_calls += 1
        type(self).last_lookup = (
            workflow_name,
            orchestrator_dir,
            dict(options or {}),
            workflow_signature,
        )
        return True

    def create_store(
        self,
        workflow_name: str,
        orchestrator_dir: Path,
        project_root: Path,
        options: Mapping[str, Any] | None = None,
    ) -> ArtifactStorePort:
        type(self).create_store_calls += 1
        resolved_options = dict(options or {})
        return OutputManager(
            workflow_name,
            base_dir=orchestrator_dir,
            template_base_dir=project_root,
            log_cli_output=bool(resolved_options.get("log_cli_output", False)),
        )


class PreflightOrderingInvoker:
    async def invoke(  # type: ignore[no-untyped-def]
        self,
        config,  # noqa: ARG002 - Required by invoker protocol.
        model,  # noqa: ARG002 - Required by invoker protocol.
        prompt,  # noqa: ARG002 - Required by invoker protocol.
        output_file,
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
            api_version=EXT_API_VERSION,
            options={},
            option_scopes={},
        )

    def create_invoker(
        self,
        config: Config,  # noqa: ARG002 - Required by adapter protocol.
        options: Mapping[str, Any] | None = None,  # noqa: ARG002 - Required by adapter protocol.
    ) -> PreflightOrderingInvoker:
        type(self).preflight_plan_exists_at_create = any(
            Path(".orchestrator").glob(
                "execution-stages/*/preflight/execution-plan.json"
            )
        )
        return PreflightOrderingInvoker()


def _mock_config(
    options: dict[str, object] | None = None,
    invoker_implementation: str = "mock",
    artifact_implementation: str = "filesystem",
    artifact_options: dict[str, object] | None = None,
) -> Config:
    resolved_artifact_options = (
        {
            "allowed_template_paths": [],
            "log_cli_output": True,
        }
        if artifact_options is None
        else artifact_options
    )
    invoker_options = dict(options or {})
    if invoker_implementation == "mock":
        invoker_options = {
            "observation_delay_seconds": 0,
            "output_mode": "echo",
            **invoker_options,
        }
    return Config(
        version=CONFIG_SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"], default_model="model-a")},
        settings=Settings(
            integrations={
                "invoker": {
                    "implementation": invoker_implementation,
                    "options": invoker_options,
                },
                "ui": {"implementation": "none", "options": {}},
                "artifacts": {
                    "implementation": artifact_implementation,
                    "options": resolved_artifact_options,
                },
            }
        ),
    )


def _workflow(prompt: str = "hello") -> WorkflowPlan:
    return WorkflowPlan(
        name="Task",
        nodes=[
            WorkflowNode(
                id="build.node",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha", role="executor")],
                prompt_segments=[PromptSegment(role="shared", content=prompt)],
            )
        ],
    )


def _workflow_payload(workflow: WorkflowPlan) -> dict[str, object]:
    return {
        "schema_version": workflow.schema_version,
        "name": workflow.name,
        "description": workflow.description,
        "inputs": dict(workflow.inputs),
        "nodes": [],
    }


def _run_dirs(root: Path) -> list[Path]:
    stages_root = root / ".orchestrator" / "execution-stages"
    if not stages_root.exists():
        return []
    return sorted(path for path in stages_root.iterdir() if path.is_dir())


def _result_dirs(root: Path) -> list[Path]:
    results_root = root / ".orchestrator" / "execution-results"
    if not results_root.exists():
        return []
    return sorted(path for path in results_root.iterdir() if path.is_dir())


async def _run_workflow(
    workflow: WorkflowPlan,
    config: Config,
    console: Console,
    force: bool = False,
    which_fn: Callable[[str], str | None] | None = None,
    execute_workflow_impl: Callable[..., Any] | None = None,
) -> None:
    run_kwargs = {}
    if execute_workflow_impl is not None:
        run_kwargs["execute_workflow_impl"] = execute_workflow_impl
    await execute_workflow_run(
        config=config,
        source=PreflightWorkflowSource.from_workflow(
            workflow,
            workflow_content="workflow source",
            composed_workflow=_workflow_payload(workflow),
        ),
        force=force,
        no_live=True,
        console=console,
        which_fn=which_fn,
        **run_kwargs,
    )


class WorkflowRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_signature_skips_without_run_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False, color_system=None)
            workflow = _workflow()
            config = _mock_config()
            original_cwd = Path.cwd()
            os.chdir(root)
            try:
                await _run_workflow(workflow, config, console)
                run_count = len(_run_dirs(root))
                await _run_workflow(workflow, config, console)
            finally:
                os.chdir(original_cwd)

            self.assertEqual(len(_run_dirs(root)), run_count)
            self.assertIn("Identical context detected", stream.getvalue())

    async def test_custom_artifact_duplicate_skips_before_store_allocation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False, color_system=None)
            workflow = _workflow()
            config = _mock_config(
                artifact_implementation=(
                    f"{__name__}:DuplicateReportingArtifactsAdapter"
                ),
                artifact_options={"marker": "duplicate"},
            )
            original_cwd = Path.cwd()
            DuplicateReportingArtifactsAdapter.reset()
            os.chdir(root)
            try:
                await _run_workflow(workflow, config, console)
            finally:
                os.chdir(original_cwd)

            self.assertEqual(
                DuplicateReportingArtifactsAdapter.workflow_signature_exists_calls,
                1,
            )
            self.assertIsNotNone(DuplicateReportingArtifactsAdapter.last_lookup)
            last_lookup = DuplicateReportingArtifactsAdapter.last_lookup
            if last_lookup is None:
                self.fail("Expected duplicate lookup inputs to be recorded.")
            workflow_name, orchestrator_dir, options, workflow_signature = last_lookup
            self.assertEqual(workflow_name, "Task")
            self.assertEqual(orchestrator_dir, root / ".orchestrator")
            self.assertEqual(options, {"marker": "duplicate"})
            self.assertEqual(len(workflow_signature), 64)
            self.assertTrue(set(workflow_signature) <= set("0123456789abcdef"))
            self.assertEqual(DuplicateReportingArtifactsAdapter.create_store_calls, 0)
            self.assertEqual(_run_dirs(root), [])
            self.assertIn("Identical context detected", stream.getvalue())

    async def test_force_ignores_duplicate_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = _workflow()
            config = _mock_config()
            original_cwd = Path.cwd()
            os.chdir(root)
            try:
                await _run_workflow(workflow, config, console)
                await _run_workflow(workflow, config, console, force=True)
            finally:
                os.chdir(original_cwd)

            self.assertEqual(len(_run_dirs(root)), 2)

    async def test_successful_run_writes_preflight_bundle_and_redacted_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = _workflow("{{env:API_TOKEN}}")
            config = _mock_config()
            config.agents["alpha"].extra_args = ["--api-key", "super-secret"]
            raw_agent_config_signature = signature_for_payload(
                config.agents["alpha"].model_dump(mode="json", exclude_none=True)
            )
            config_yaml_content = "api_token: super-secret\n"
            raw_config_yaml_signature = hashlib.sha256(
                config_yaml_content.encode("utf-8")
            ).hexdigest()
            original_cwd = Path.cwd()
            original_api_token = os.environ.get("API_TOKEN")
            os.chdir(root)
            os.environ["API_TOKEN"] = "env-secret"
            try:
                await _run_workflow(workflow, config, console)
            finally:
                if original_api_token is None:
                    os.environ.pop("API_TOKEN", None)
                else:
                    os.environ["API_TOKEN"] = original_api_token
                os.chdir(original_cwd)

            run_dirs = _run_dirs(root)
            self.assertEqual(len(run_dirs), 1)
            self.assertEqual(len(_result_dirs(root)), 1)
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
            self.assertTrue(
                expected_preflight_files.issubset(
                    {path.name for path in preflight_dir.iterdir()}
                )
            )
            preflight_manifest = json.loads(
                (preflight_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(preflight_manifest["status"], "preflight_succeeded")
            plan_text = (preflight_dir / "execution-plan.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(raw_agent_config_signature, plan_text)
            self.assertNotIn("env-secret", plan_text)
            plan = json.loads(plan_text)
            self.assertEqual(
                plan["runtime_config_snapshot"]["sensitive_config_paths"],
                ["agents.alpha.extra_args.1"],
            )
            self.assertGreaterEqual(len(plan["value_fingerprints"]), 1)
            self.assertTrue(
                all("value" not in record for record in plan["value_fingerprints"])
            )
            self.assertEqual(plan["value_fingerprints"][0]["key"], "API_TOKEN")
            self.assertEqual(len(plan["value_fingerprints"][0]["fingerprint"]), 64)
            execution_bundle = json.loads(
                (preflight_dir / "execution-bundle.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                execution_bundle["value_fingerprints"],
                plan["value_fingerprints"],
            )
            manifest_path = (
                run_dirs[0] / "manifests" / f"{plan['workflow_signature']}.json"
            )
            manifest_text = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)

            self.assertNotIn("config_yaml", manifest)
            self.assertNotIn("config_yaml_sha256", manifest)
            self.assertNotIn("super-secret", manifest_text)
            self.assertNotIn("env-secret", manifest_text)
            self.assertNotIn(raw_config_yaml_signature, manifest_text)
            self.assertNotIn(raw_agent_config_signature, manifest_text)
            self.assertEqual(
                manifest["runtime_config_snapshot"],
                plan["runtime_config_snapshot"],
            )

    async def test_runtime_receives_preflight_plan_agent_configs_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = _workflow()
            config = _mock_config()
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
            ) -> None:
                captured_command.extend(
                    plan.runtime_config_snapshot["agents"]["alpha"]["cli_cmd"]
                )

            original_cwd = Path.cwd()
            os.chdir(root)
            try:
                await _run_workflow(
                    workflow,
                    config,
                    console,
                    execute_workflow_impl=fake_execute_workflow,
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(captured_command, ["preflight-command"])

    async def test_preflight_failure_writes_failure_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = _workflow("{{env:MISSING_REQUIRED_ENV}}")
            config = _mock_config()
            original_env = os.environ.pop("MISSING_REQUIRED_ENV", None)
            original_cwd = Path.cwd()
            os.chdir(root)
            try:
                with self.assertRaises(typer.Exit):
                    await _run_workflow(workflow, config, console)
            finally:
                if original_env is not None:
                    os.environ["MISSING_REQUIRED_ENV"] = original_env
                os.chdir(original_cwd)

            run_dirs = _run_dirs(root)
            self.assertEqual(len(run_dirs), 1)
            preflight_dir = run_dirs[0] / "preflight"
            self.assertTrue((preflight_dir / "diagnostics.json").exists())
            self.assertTrue((preflight_dir / "metadata.json").exists())
            self.assertTrue((preflight_dir / "manifest.json").exists())
            failure_manifest = json.loads(
                (preflight_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(failure_manifest["status"], "preflight_failed")
            self.assertFalse((run_dirs[0] / "manifests").exists())
            self.assertEqual(_result_dirs(root), [])

    async def test_cli_availability_failure_writes_preflight_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = _workflow()
            config = _mock_config(invoker_implementation="cli")
            original_cwd = Path.cwd()

            def missing_cli(command: str) -> str | None:
                self.assertTrue(command)
                return None

            os.chdir(root)
            try:
                with self.assertRaises(typer.Exit):
                    await _run_workflow(
                        workflow,
                        config,
                        console,
                        which_fn=missing_cli,
                    )
            finally:
                os.chdir(original_cwd)

            run_dirs = _run_dirs(root)
            self.assertEqual(len(run_dirs), 1)
            self.assertEqual(_result_dirs(root), [])
            diagnostics = json.loads(
                (run_dirs[0] / "preflight" / "diagnostics.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(diagnostics[0]["code"], "PROVIDER-CLI")
            self.assertIn("not found in PATH", diagnostics[0]["message"])
            failure_manifest = json.loads(
                (run_dirs[0] / "preflight" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(failure_manifest["status"], "preflight_failed")

    async def test_runtime_config_snapshot_failure_writes_failure_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = _workflow()
            config = _mock_config({"unknown_option": True})
            original_cwd = Path.cwd()
            os.chdir(root)
            try:
                with self.assertRaises(typer.Exit):
                    await _run_workflow(workflow, config, console)
            finally:
                os.chdir(original_cwd)

            run_dirs = _run_dirs(root)
            self.assertEqual(len(run_dirs), 1)
            self.assertEqual(_result_dirs(root), [])
            diagnostics = json.loads(
                (run_dirs[0] / "preflight" / "diagnostics.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(diagnostics[0]["code"], "RUNTIME-CONFIG")
            failure_manifest = json.loads(
                (run_dirs[0] / "preflight" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(failure_manifest["status"], "preflight_failed")

    async def test_preflight_plan_is_materialized_before_invoker_construction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            console = Console(file=io.StringIO(), force_terminal=False)
            workflow = _workflow()
            config = _mock_config(
                invoker_implementation=(f"{__name__}:PreflightOrderingInvokerAdapter"),
                options={},
            )
            original_cwd = Path.cwd()
            PreflightOrderingInvokerAdapter.reset()
            os.chdir(root)
            try:
                await _run_workflow(workflow, config, console)
            finally:
                os.chdir(original_cwd)

            self.assertTrue(
                PreflightOrderingInvokerAdapter.preflight_plan_exists_at_create
            )

    def test_early_preflight_failure_uses_fallback_run_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            original_cwd = Path.cwd()
            os.chdir(root)
            try:
                write_early_preflight_failure_run(
                    root / "bad workflow.task.md",
                    "frontmatter failed",
                )
            finally:
                os.chdir(original_cwd)

            run_dirs = _run_dirs(root)
            self.assertEqual(len(run_dirs), 1)
            self.assertEqual(_result_dirs(root), [])
            self.assertTrue(run_dirs[0].name.startswith("bad-workflow-"))
            metadata = json.loads(
                (run_dirs[0] / "preflight" / "metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsNone(metadata["workflow_name"])


if __name__ == "__main__":
    unittest.main()
