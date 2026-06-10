import io
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

from rich.console import Console

import orchestrator_cli.adapters.ui.tmux as tmux_adapter_module
from orchestrator_cli.architecture.errors import AdapterLoadError
from orchestrator_cli.architecture.ports.runtime import UIRuntimePlan
from orchestrator_cli.bootstrap import (
    build_runtime_components,
    build_runtime_config_snapshot,
)
from orchestrator_cli.core.config import AgentConfig, Config, Settings
from orchestrator_cli.core.preflight import (
    PreflightCompileOptions,
    PreflightExecutionPlan,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.runtime.execution import execute_workflow
from orchestrator_cli.version import SCHEMA_VERSION
from tests.helpers.observability import topology_from_workflow


def _compiled_test_plan(
    config: Config,
    workflow: WorkflowPlan,
    components,
) -> tuple[PreflightExecutionPlan, object]:
    output = components.artifact_store
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )
    preview = compile_preflight_preview(
        source=PreflightWorkflowSource.from_workflow(
            workflow,
            workflow_content="test workflow",
            composed_workflow={
                "schema_version": workflow.schema_version,
                "name": workflow.name,
                "description": workflow.description,
                "inputs": dict(workflow.inputs),
                "nodes": [],
            },
        ),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=output.base_dir,
            orchestrator_dir=output.base_dir,
            fingerprint_key_policy="read_only",
        ),
    )
    if preview.diagnostics:
        diagnostics = "; ".join(
            f"{diagnostic.code}: {diagnostic.message}"
            for diagnostic in preview.diagnostics
        )
        raise AssertionError(f"Unexpected preflight diagnostics: {diagnostics}")
    plan = PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        context_root=output.stages_dir.as_posix(),
        manifest_root=(output.stages_dir / "manifests").as_posix(),
        created_at=datetime(2026, 6, 3),
    )
    for content_ref, payload in preview.static_file_payloads.items():
        output.write_preflight_static_file(content_ref, payload)
    return plan, preview.secret_context


class ContainerTests(unittest.TestCase):
    def _build_config(self, settings: Settings | None = None) -> Config:
        return Config(
            version=SCHEMA_VERSION,
            agents={
                "alpha": AgentConfig(cli_cmd=["echo"], default_model="model-a"),
            },
            settings=settings,
        )

    def _build_workflow(self) -> WorkflowPlan:
        return WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="run")],
                    providers=[ProviderSpec(provider="alpha", role="executor")],
                )
            ],
        )

    def test_container_builds_default_components_without_live_ui(self) -> None:
        workflow = self._build_workflow()
        config = self._build_config()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            components = build_runtime_components(
                config=config,
                workflow_topology=topology_from_workflow(workflow),
                orchestrator_dir=tmp_path,
                project_root=tmp_path,
                console=Console(file=io.StringIO(), force_terminal=False),
                no_live=False,
            )

        self.assertEqual(components.observers, ())
        self.assertEqual(components.artifact_store.task_name, "workflow")
        self.assertIsNotNone(components.base_invoker)

    def test_runtime_plan_normalizes_observers_to_immutable_tuple(self) -> None:
        observer = object()
        runtime_plan = UIRuntimePlan(
            observers=[observer],
            suppress_progress_output=False,
        )

        self.assertEqual(runtime_plan.observers, (observer,))
        with self.assertRaises(FrozenInstanceError):
            runtime_plan.observers = ()  # type: ignore[misc]

    def test_container_respects_none_ui_integration(self) -> None:
        workflow = self._build_workflow()
        config = self._build_config(
            Settings(
                integrations={
                    "invoker": {"implementation": "cli", "options": {}},
                    "ui": {"implementation": "none", "options": {}},
                    "artifacts": {
                        "implementation": "filesystem",
                        "options": {
                            "log_cli_output": True,
                            "allowed_template_paths": [],
                        },
                    },
                }
            )
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            components = build_runtime_components(
                config=config,
                workflow_topology=topology_from_workflow(workflow),
                orchestrator_dir=tmp_path,
                project_root=tmp_path,
                console=Console(file=io.StringIO(), force_terminal=True),
                no_live=False,
            )

        self.assertEqual(components.observers, ())
        self.assertFalse(hasattr(components, "invoker_override"))

    def test_container_accepts_dotted_override_for_ui(self) -> None:
        workflow = self._build_workflow()
        config = self._build_config(
            Settings(
                integrations={
                    "invoker": {"implementation": "cli", "options": {}},
                    "ui": {
                        "implementation": "orchestrator_cli.adapters.ui.null:NullUIAdapter",
                        "options": {},
                    },
                    "artifacts": {
                        "implementation": "filesystem",
                        "options": {
                            "log_cli_output": True,
                            "allowed_template_paths": [],
                        },
                    },
                }
            )
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            components = build_runtime_components(
                config=config,
                workflow_topology=topology_from_workflow(workflow),
                orchestrator_dir=tmp_path,
                project_root=tmp_path,
                console=Console(file=io.StringIO(), force_terminal=True),
                no_live=False,
            )

        self.assertEqual(components.observers, ())

    def test_container_skips_ui_adapter_loading_when_live_ui_disabled(self) -> None:
        workflow = self._build_workflow()
        config = self._build_config(
            Settings(
                integrations={
                    "invoker": {"implementation": "cli", "options": {}},
                    "ui": {
                        "implementation": "missing.module:MissingUIAdapter",
                        "options": {},
                    },
                    "artifacts": {
                        "implementation": "filesystem",
                        "options": {
                            "log_cli_output": True,
                            "allowed_template_paths": [],
                        },
                    },
                }
            )
        )

        scenarios = [
            (
                "non_tty",
                Console(file=io.StringIO(), force_terminal=False),
                False,
            ),
            (
                "no_live",
                Console(file=io.StringIO(), force_terminal=True),
                True,
            ),
        ]
        for name, console, no_live in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                components = build_runtime_components(
                    config=config,
                    workflow_topology=topology_from_workflow(workflow),
                    orchestrator_dir=tmp_path,
                    project_root=tmp_path,
                    console=console,
                    no_live=no_live,
                )

            self.assertEqual(components.observers, ())
            self.assertIsNotNone(components.base_invoker)

    def test_container_requires_ui_adapter_when_live_ui_enabled(self) -> None:
        workflow = self._build_workflow()
        config = self._build_config(
            Settings(
                integrations={
                    "invoker": {"implementation": "cli", "options": {}},
                    "ui": {
                        "implementation": "missing.module:MissingUIAdapter",
                        "options": {},
                    },
                    "artifacts": {
                        "implementation": "filesystem",
                        "options": {
                            "log_cli_output": True,
                            "allowed_template_paths": [],
                        },
                    },
                }
            )
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with self.assertRaisesRegex(AdapterLoadError, "missing.module"):
                build_runtime_components(
                    config=config,
                    workflow_topology=topology_from_workflow(workflow),
                    orchestrator_dir=tmp_path,
                    project_root=tmp_path,
                    console=Console(file=io.StringIO(), force_terminal=True),
                    no_live=False,
                )

    def test_container_validates_invoker_before_allocating_run_directories(
        self,
    ) -> None:
        workflow = self._build_workflow()
        config = self._build_config(
            Settings(
                integrations={
                    "invoker": {"implementation": "missing.module:MissingInvoker"},
                    "ui": {"implementation": "none", "options": {}},
                    "artifacts": {
                        "implementation": "filesystem",
                        "options": {
                            "log_cli_output": True,
                            "allowed_template_paths": [],
                        },
                    },
                }
            )
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with self.assertRaisesRegex(AdapterLoadError, "missing.module"):
                build_runtime_components(
                    config=config,
                    workflow_topology=topology_from_workflow(workflow),
                    orchestrator_dir=tmp_path,
                    project_root=tmp_path,
                    console=Console(file=io.StringIO(), force_terminal=False),
                    no_live=False,
                )

            self.assertFalse((tmp_path / "execution-stages").exists())
            self.assertFalse((tmp_path / "execution-results").exists())

    def test_container_passes_default_tmux_liveness_options_to_runtime(self) -> None:
        workflow = self._build_workflow()
        config = self._build_config()
        captured: dict[str, object] = {}
        components = None
        original_runtime_class = tmux_adapter_module.TmuxCompactRuntime

        class StubRuntime:
            def __init__(  # type: ignore[no-untyped-def]
                self,
                auto_close_session=True,
                tmux_executable="tmux",
                quiet_after_seconds=120.0,
                log_tail_lines=None,
                warning_sink=None,  # noqa: ARG002 - Required by callback or protocol signature.
            ):
                captured["auto_close_session"] = auto_close_session
                captured["tmux_executable"] = tmux_executable
                captured["quiet_after_seconds"] = quiet_after_seconds
                captured["log_tail_lines"] = log_tail_lines

        tmux_adapter_module.TmuxCompactRuntime = StubRuntime  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                components = build_runtime_components(
                    config=config,
                    workflow_topology=topology_from_workflow(workflow),
                    orchestrator_dir=tmp_path,
                    project_root=tmp_path,
                    console=Console(file=io.StringIO(), force_terminal=True),
                    no_live=False,
                    which_fn=lambda executable: "/usr/bin/tmux",  # noqa: ARG005 - Required by callback or protocol signature.
                )
        finally:
            tmux_adapter_module.TmuxCompactRuntime = original_runtime_class  # type: ignore[assignment]

        self.assertIsNotNone(components)
        self.assertEqual(len(components.observers), 1)
        self.assertEqual(captured["auto_close_session"], True)
        self.assertEqual(captured["tmux_executable"], "tmux")
        self.assertEqual(captured["quiet_after_seconds"], 120.0)
        self.assertIsNone(captured["log_tail_lines"])

    def test_container_disables_tmux_live_ui_when_logs_are_disabled(self) -> None:
        workflow = self._build_workflow()
        config = self._build_config(
            Settings(
                integrations={
                    "invoker": {"implementation": "cli", "options": {}},
                    "ui": {"implementation": "tmux", "options": {}},
                    "artifacts": {
                        "implementation": "filesystem",
                        "options": {
                            "log_cli_output": False,
                            "allowed_template_paths": [],
                        },
                    },
                }
            )
        )
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            components = build_runtime_components(
                config=config,
                workflow_topology=topology_from_workflow(workflow),
                orchestrator_dir=tmp_path,
                project_root=tmp_path,
                console=Console(file=io.StringIO(), force_terminal=True),
                no_live=False,
                warning_sink=warnings.append,
                which_fn=lambda executable: "/usr/bin/tmux",  # noqa: ARG005 - Required by callback or protocol signature.
            )

        self.assertEqual(components.observers, ())
        self.assertIsNotNone(components.base_invoker)
        self.assertTrue(warnings)
        self.assertIn("log_cli_output=true", warnings[-1])

    def test_container_passes_runtime_dependencies_to_dotted_tmux_override(
        self,
    ) -> None:
        workflow = self._build_workflow()
        config = self._build_config(
            Settings(
                integrations={
                    "invoker": {"implementation": "cli", "options": {}},
                    "ui": {
                        "implementation": (
                            "orchestrator_cli.adapters.ui.tmux:TmuxUIAdapter"
                        ),
                        "options": {},
                    },
                    "artifacts": {
                        "implementation": "filesystem",
                        "options": {
                            "log_cli_output": True,
                            "allowed_template_paths": [],
                        },
                    },
                }
            )
        )
        captured: dict[str, object] = {}
        components = None
        original_runtime_class = tmux_adapter_module.TmuxCompactRuntime

        class StubRuntime:
            def __init__(  # type: ignore[no-untyped-def]
                self,
                auto_close_session=True,
                tmux_executable="tmux",
                quiet_after_seconds=120.0,
                log_tail_lines=None,
                warning_sink=None,  # noqa: ARG002 - Required by callback or protocol signature.
            ):
                captured["auto_close_session"] = auto_close_session
                captured["tmux_executable"] = tmux_executable
                captured["quiet_after_seconds"] = quiet_after_seconds
                captured["log_tail_lines"] = log_tail_lines

        tmux_adapter_module.TmuxCompactRuntime = StubRuntime  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                components = build_runtime_components(
                    config=config,
                    workflow_topology=topology_from_workflow(workflow),
                    orchestrator_dir=tmp_path,
                    project_root=tmp_path,
                    console=Console(file=io.StringIO(), force_terminal=True),
                    no_live=False,
                    which_fn=lambda executable: "/usr/bin/tmux",  # noqa: ARG005 - Required by callback or protocol signature.
                )
        finally:
            tmux_adapter_module.TmuxCompactRuntime = original_runtime_class  # type: ignore[assignment]

        self.assertIsNotNone(components)
        self.assertEqual(len(components.observers), 1)
        self.assertEqual(captured["auto_close_session"], True)
        self.assertEqual(captured["tmux_executable"], "tmux")
        self.assertEqual(captured["quiet_after_seconds"], 120.0)
        self.assertIsNone(captured["log_tail_lines"])

    def test_container_disables_dotted_tmux_live_ui_when_logs_are_disabled(
        self,
    ) -> None:
        workflow = self._build_workflow()
        config = self._build_config(
            Settings(
                integrations={
                    "invoker": {"implementation": "cli", "options": {}},
                    "ui": {
                        "implementation": (
                            "orchestrator_cli.adapters.ui.tmux:TmuxUIAdapter"
                        ),
                        "options": {},
                    },
                    "artifacts": {
                        "implementation": "filesystem",
                        "options": {
                            "log_cli_output": False,
                            "allowed_template_paths": [],
                        },
                    },
                }
            )
        )
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            components = build_runtime_components(
                config=config,
                workflow_topology=topology_from_workflow(workflow),
                orchestrator_dir=tmp_path,
                project_root=tmp_path,
                console=Console(file=io.StringIO(), force_terminal=True),
                no_live=False,
                warning_sink=warnings.append,
                which_fn=lambda executable: "/usr/bin/tmux",  # noqa: ARG005 - Required by callback or protocol signature.
            )

            self.assertEqual(components.observers, ())
        self.assertIsNotNone(components.base_invoker)
        self.assertTrue(warnings)
        self.assertIn("log_cli_output=true", warnings[-1])


class ContainerRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_invoker_runs_workflow_without_provider_cli(self) -> None:
        workflow = WorkflowPlan(
            name="mock-runtime",
            nodes=[
                WorkflowNode(
                    id="node.mock",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(
                            role="shared", content="Generate a deterministic summary."
                        )
                    ],
                    providers=[ProviderSpec(provider="alpha", role="executor")],
                )
            ],
        )
        config = Config(
            version=SCHEMA_VERSION,
            agents={"alpha": AgentConfig(cli_cmd=["echo"], default_model="model-a")},
            settings=Settings(
                integrations={
                    "invoker": {
                        "implementation": "mock",
                        "options": {
                            "observation_delay_seconds": 0,
                            "output_mode": "lorem",
                            "seed": 42,
                        },
                    },
                    "ui": {"implementation": "none", "options": {}},
                    "artifacts": {
                        "implementation": "filesystem",
                        "options": {
                            "log_cli_output": True,
                            "allowed_template_paths": [],
                        },
                    },
                }
            ),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            components = build_runtime_components(
                config=config,
                workflow_topology=topology_from_workflow(workflow),
                orchestrator_dir=tmp_path,
                project_root=tmp_path,
                console=Console(file=io.StringIO(), force_terminal=False),
                no_live=True,
            )
            plan, secret_context = _compiled_test_plan(config, workflow, components)
            await execute_workflow(
                plan=plan,
                output=components.artifact_store,
                invoker=components.base_invoker,
                secret_context=secret_context,
                suppress_progress_output=True,
            )

            result_file = components.artifact_store.get_stage_output_path("node.mock")
            self.assertTrue(result_file.exists())
            output_text = result_file.read_text(encoding="utf-8")
            self.assertIn("# Mock Invocation Output", output_text)
            self.assertIn("- Node: node.mock", output_text)
            self.assertEqual(components.artifact_store.task_name, "mock-runtime")
            self.assertIsNotNone(components.base_invoker)
        self.assertFalse(hasattr(components, "invoker_override"))

    async def test_mock_invoker_writes_findings_artifact_for_lorem_output(
        self,
    ) -> None:
        workflow = WorkflowPlan(
            name="mock-findings-runtime",
            nodes=[
                WorkflowNode(
                    id="review.context",
                    mode="sequential",
                    findings=True,
                    prompt_segments=[
                        PromptSegment(
                            role="shared",
                            content="Review the repository and summarize the result.",
                        )
                    ],
                    providers=[ProviderSpec(provider="alpha", role="executor")],
                )
            ],
        )
        config = Config(
            version=SCHEMA_VERSION,
            agents={"alpha": AgentConfig(cli_cmd=["echo"], default_model="model-a")},
            settings=Settings(
                integrations={
                    "invoker": {
                        "implementation": "mock",
                        "options": {
                            "observation_delay_seconds": 0,
                            "output_mode": "lorem",
                            "seed": 42,
                        },
                    },
                    "ui": {"implementation": "none", "options": {}},
                    "artifacts": {
                        "implementation": "filesystem",
                        "options": {
                            "log_cli_output": True,
                            "allowed_template_paths": [],
                        },
                    },
                }
            ),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            components = build_runtime_components(
                config=config,
                workflow_topology=topology_from_workflow(workflow),
                orchestrator_dir=tmp_path,
                project_root=tmp_path,
                console=Console(file=io.StringIO(), force_terminal=False),
                no_live=True,
            )
            plan, secret_context = _compiled_test_plan(config, workflow, components)
            await execute_workflow(
                plan=plan,
                output=components.artifact_store,
                invoker=components.base_invoker,
                secret_context=secret_context,
                suppress_progress_output=True,
            )

            result_file = components.artifact_store.get_stage_output_path(
                "review.context"
            )
            findings_file = components.artifact_store.get_stage_findings_path(
                "review.context"
            )
            self.assertTrue(result_file.exists())
            self.assertTrue(findings_file.exists())
            self.assertIn(
                "Synthetic finding for review.context",
                findings_file.read_text(encoding="utf-8"),
            )
