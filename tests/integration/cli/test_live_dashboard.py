import io
import os
import tempfile
import unittest
from pathlib import Path

import typer

import orchestrator_cli.cli.app as cli
from orchestrator_cli.version import SCHEMA_VERSION
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    write_basic_config,
    write_basic_workflow,
)


class CliLiveDashboardTests(unittest.TestCase):
    def test_init_creates_workflow_template_without_legacy_tasks_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            original_cwd = Path.cwd()
            os.chdir(tmp_path)
            try:
                cli.init()
            finally:
                os.chdir(original_cwd)

            orch_dir = tmp_path / ".orchestrator"
            workflows_dir = orch_dir / "workflows"
            library_dir = workflows_dir / "example-templates"
            composition_dir = library_dir / "composition"
            self.assertTrue((orch_dir / "config.yml").exists())
            self.assertTrue((workflows_dir / "code-review-example.task.md").exists())
            self.assertFalse((workflows_dir / "example.task.md").exists())
            self.assertTrue((library_dir / "design-review-example.task.md").exists())
            self.assertTrue(
                (library_dir / "feature-implement-example.task.md").exists()
            )
            self.assertTrue((library_dir / "test-generation-example.task.md").exists())
            self.assertTrue((library_dir / "refactoring-example.task.md").exists())
            self.assertTrue(
                (composition_dir / "review-findings-producer-example.task.md").exists()
            )
            self.assertTrue(
                (composition_dir / "review-fix-consumer-example.task.md").exists()
            )
            self.assertTrue(
                (composition_dir / "review-fix-composed-example.task.md").exists()
            )
            self.assertFalse((orch_dir / "inputs").exists())
            sample_input_dir = library_dir / "sample-inputs"
            self.assertTrue((sample_input_dir / "feature-brief.md").exists())
            self.assertTrue((sample_input_dir / "review-findings.md").exists())
            self.assertTrue((sample_input_dir / "coding-standards.md").exists())
            self.assertFalse((orch_dir / "tasks.yaml").exists())
            config_text = (orch_dir / "config.yml").read_text(encoding="utf-8")
            workflow_text = (workflows_dir / "code-review-example.task.md").read_text(
                encoding="utf-8"
            )
            top_level_workflow_files = sorted(workflows_dir.glob("*.task.md"))
            self.assertEqual(
                [path.name for path in top_level_workflow_files],
                ["code-review-example.task.md"],
            )
            self.assertIn(f'version: "{SCHEMA_VERSION}"', config_text)
            self.assertIn(f'schema_version: "{SCHEMA_VERSION}"', workflow_text)
            self.assertNotIn("__SCHEMA_VERSION__", config_text)
            self.assertNotIn("__SCHEMA_VERSION__", workflow_text)
            self.assertIn("--dangerously-skip-permissions", config_text)
            self.assertIn(
                "--dangerously-bypass-approvals-and-sandbox",
                config_text,
            )
            self.assertIn("--approval-mode=yolo", config_text)
            self.assertIn('cli_cmd: ["copilot"]', config_text)
            self.assertIn("--no-ask-user", config_text)

    def test_non_tty_run_uses_compact_fallback_without_live_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_execute_workflow = cli.execute_workflow
            original_cwd = Path.cwd()
            captured_kwargs: dict[str, object] = {}

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                captured_kwargs.update(kwargs)

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            os.chdir(tmp_path)
            try:
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
            finally:
                os.chdir(original_cwd)
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]
                cli.Console = original_console_cls

            self.assertIn("invoker", captured_kwargs)
            self.assertIn("event_sink", captured_kwargs)
            self.assertIn("run_id", captured_kwargs)

    def test_tty_run_enables_live_dashboard_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_execute_workflow = cli.execute_workflow
            original_hub = cli.ObservabilityHub
            original_which = cli.shutil.which
            original_cwd = Path.cwd()
            captured_kwargs: dict[str, object] = {}
            captured_live_config: dict[str, object] = {}
            import orchestrator_cli.adapters.ui.tmux as tmux_adapter_module

            original_runtime_class = tmux_adapter_module.TmuxCompactRuntime

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                captured_kwargs.update(kwargs)

            class StubObserver:
                def __init__(  # type: ignore[no-untyped-def]
                    self,
                    auto_close_session=True,
                    tmux_executable="tmux",  # noqa: ARG002 - Required by test double or callback signature.
                    quiet_after_seconds=120.0,  # noqa: ARG002 - Required by test double or callback signature.
                    log_tail_lines=None,
                    warning_sink=None,  # noqa: ARG002 - Required by test double or callback signature.
                ):
                    self.auto_close_session = auto_close_session
                    captured_live_config["log_tail_lines"] = log_tail_lines

            class StubHub:
                def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by test double or callback signature.
                    self.active_observer_count = 2
                    self.stop_requested = False

                def __enter__(self):  # type: ignore[no-untyped-def]
                    return self

                def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
                    return None

                def emit(self, event):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by test double or callback signature.
                    return None

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=True,
                color_system=None,
                width=120,
                height=40,
            )
            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            cli.ObservabilityHub = StubHub  # type: ignore[assignment]
            tmux_adapter_module.TmuxCompactRuntime = StubObserver  # type: ignore[assignment]
            cli.shutil.which = (  # type: ignore[assignment]
                lambda value: (
                    "/usr/bin/tmux" if value == "tmux" else original_which(value)
                )
            )
            os.chdir(tmp_path)
            try:
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
            finally:
                os.chdir(original_cwd)
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]
                cli.ObservabilityHub = original_hub  # type: ignore[assignment]
                tmux_adapter_module.TmuxCompactRuntime = original_runtime_class  # type: ignore[assignment]
                cli.shutil.which = original_which  # type: ignore[assignment]
                cli.Console = original_console_cls

            self.assertIn("event_sink", captured_kwargs)
            self.assertIn("invoker", captured_kwargs)
            self.assertEqual(captured_kwargs.get("suppress_progress_output"), True)
            self.assertIn("run_id", captured_kwargs)
            self.assertIsNone(captured_live_config["log_tail_lines"])

    def test_run_exits_cleanly_when_live_dashboard_requests_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_execute_workflow_run = cli.workflow_runner.execute_workflow_run
            original_cwd = Path.cwd()

            async def fake_execute_workflow_run(**kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                raise cli.workflow_runner.WorkflowCancelledByUser(
                    "Workflow cancelled by live dashboard quit request."
                )

            cli.Console = ConsoleFactory(file=stream, force_terminal=False)
            cli.workflow_runner.execute_workflow_run = fake_execute_workflow_run  # type: ignore[assignment]
            os.chdir(tmp_path)
            try:
                with self.assertRaises(typer.Exit) as raised:
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                        force=False,
                    )
            finally:
                os.chdir(original_cwd)
                cli.workflow_runner.execute_workflow_run = original_execute_workflow_run  # type: ignore[assignment]
                cli.Console = original_console_cls

            self.assertEqual(raised.exception.exit_code, 130)
            self.assertIn(
                "Workflow cancelled by live dashboard quit request.",
                stream.getvalue(),
            )

    def test_tty_live_dashboard_uses_configured_tmux_auto_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            config_path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "",
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                        '    default_model: "model-a"',
                        "settings:",
                        "  integrations:",
                        "    ui:",
                        '      implementation: "tmux"',
                        "      options:",
                        "        auto_close_session: false",
                        "        quiet_after_seconds: 180",
                        "        log_tail_lines: 25",
                    ]
                ),
                encoding="utf-8",
            )
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_hub = cli.ObservabilityHub
            original_which = cli.shutil.which
            original_execute_workflow = cli.execute_workflow
            original_cwd = Path.cwd()
            captured_live_config: dict[str, object] = {}
            import orchestrator_cli.adapters.ui.tmux as tmux_adapter_module

            original_runtime_class = tmux_adapter_module.TmuxCompactRuntime

            class StubObserver:
                def __init__(  # type: ignore[no-untyped-def]
                    self,
                    auto_close_session=True,
                    tmux_executable="tmux",  # noqa: ARG002 - Required by test double or callback signature.
                    quiet_after_seconds=120.0,
                    log_tail_lines=None,
                    warning_sink=None,  # noqa: ARG002 - Required by test double or callback signature.
                ):
                    captured_live_config["auto_close_session"] = auto_close_session
                    captured_live_config["quiet_after_seconds"] = quiet_after_seconds
                    captured_live_config["log_tail_lines"] = log_tail_lines

            class StubHub:
                def __init__(
                    self,
                    workflow_topology,  # noqa: ARG002 - Required by test double or callback signature.
                    run_id,  # noqa: ARG002 - Required by test double or callback signature.
                    observers,
                    refresh_per_second,
                    warning_sink,  # noqa: ARG002 - Required by test double or callback signature.
                ):  # type: ignore[no-untyped-def]
                    self.active_observer_count = 2
                    self.stop_requested = False
                    captured_live_config["observer_count"] = len(observers)
                    captured_live_config["refresh_per_second"] = refresh_per_second

                def __enter__(self):  # type: ignore[no-untyped-def]
                    return self

                def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
                    return None

                def emit(self, event):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by test double or callback signature.
                    return None

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                return None

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=True,
                color_system=None,
                width=120,
                height=40,
            )
            tmux_adapter_module.TmuxCompactRuntime = StubObserver  # type: ignore[assignment]
            cli.ObservabilityHub = StubHub  # type: ignore[assignment]
            cli.shutil.which = (  # type: ignore[assignment]
                lambda value: (
                    "/usr/bin/tmux" if value == "tmux" else original_which(value)
                )
            )
            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            os.chdir(tmp_path)
            try:
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
            finally:
                os.chdir(original_cwd)
                tmux_adapter_module.TmuxCompactRuntime = original_runtime_class  # type: ignore[assignment]
                cli.ObservabilityHub = original_hub  # type: ignore[assignment]
                cli.shutil.which = original_which  # type: ignore[assignment]
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]
                cli.Console = original_console_cls

            self.assertEqual(captured_live_config["observer_count"], 2)
            self.assertEqual(captured_live_config["refresh_per_second"], 0)
            self.assertEqual(captured_live_config["auto_close_session"], False)
            self.assertEqual(captured_live_config["quiet_after_seconds"], 180.0)
            self.assertEqual(captured_live_config["log_tail_lines"], 25)

    def test_tty_run_no_live_flag_disables_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_execute_workflow = cli.execute_workflow
            original_cwd = Path.cwd()
            captured_kwargs: dict[str, object] = {}

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                captured_kwargs.update(kwargs)

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=True,
                color_system=None,
                width=120,
                height=40,
            )
            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            os.chdir(tmp_path)
            try:
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                    no_live=True,
                )
            finally:
                os.chdir(original_cwd)
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]
                cli.Console = original_console_cls

            self.assertIn("invoker", captured_kwargs)
            self.assertIn("event_sink", captured_kwargs)
            self.assertIn("run_id", captured_kwargs)

    def test_tty_run_falls_back_when_tmux_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_execute_workflow = cli.execute_workflow
            original_which = cli.shutil.which
            original_cwd = Path.cwd()
            captured_kwargs: dict[str, object] = {}

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                captured_kwargs.update(kwargs)

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=True,
                color_system=None,
                width=120,
                height=40,
            )
            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            cli.shutil.which = (  # type: ignore[assignment]
                lambda value: None if value == "tmux" else original_which(value)
            )
            os.chdir(tmp_path)
            try:
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
            finally:
                os.chdir(original_cwd)
                cli.shutil.which = original_which  # type: ignore[assignment]
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]
                cli.Console = original_console_cls

            self.assertIn("invoker", captured_kwargs)
            self.assertIn("event_sink", captured_kwargs)
            self.assertIn("tmux not found", stream.getvalue())

    def test_tty_run_falls_back_when_no_live_observers_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_execute_workflow = cli.execute_workflow
            original_hub = cli.ObservabilityHub
            original_which = cli.shutil.which
            original_cwd = Path.cwd()
            captured_kwargs: dict[str, object] = {}
            import orchestrator_cli.adapters.ui.tmux as tmux_adapter_module

            original_runtime_class = tmux_adapter_module.TmuxCompactRuntime

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                captured_kwargs.update(kwargs)

            class StubObserver:
                def __init__(  # type: ignore[no-untyped-def]
                    self,
                    auto_close_session=True,
                    tmux_executable="tmux",
                    warning_sink=None,
                    quiet_after_seconds=120.0,
                    log_tail_lines=None,
                ):
                    pass

            class StubHub:
                def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by test double or callback signature.
                    self.active_observer_count = 0
                    self.stop_requested = False

                def __enter__(self):  # type: ignore[no-untyped-def]
                    return self

                def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
                    return None

                def emit(self, event):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by test double or callback signature.
                    return None

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=True,
                color_system=None,
                width=120,
                height=40,
            )
            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            cli.ObservabilityHub = StubHub  # type: ignore[assignment]
            tmux_adapter_module.TmuxCompactRuntime = StubObserver  # type: ignore[assignment]
            cli.shutil.which = (  # type: ignore[assignment]
                lambda value: (
                    "/usr/bin/tmux" if value == "tmux" else original_which(value)
                )
            )
            os.chdir(tmp_path)
            try:
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
            finally:
                os.chdir(original_cwd)
                cli.shutil.which = original_which  # type: ignore[assignment]
                tmux_adapter_module.TmuxCompactRuntime = original_runtime_class  # type: ignore[assignment]
                cli.ObservabilityHub = original_hub  # type: ignore[assignment]
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]
                cli.Console = original_console_cls

            self.assertIn("invoker", captured_kwargs)
            self.assertIn("event_sink", captured_kwargs)
            self.assertIn("live dashboard unavailable", stream.getvalue())
