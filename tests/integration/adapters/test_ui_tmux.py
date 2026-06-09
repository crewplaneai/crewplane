import io
import unittest

from rich.console import Console

import orchestrator_cli.adapters.ui.tmux as tmux_adapter_module
from orchestrator_cli.adapters.ui.tmux import TmuxUIAdapter
from orchestrator_cli.core.config import Config
from orchestrator_cli.observability.types import WorkflowTopology
from orchestrator_cli.versions import CONFIG_SCHEMA_VERSION


class TmuxUIAdapterTests(unittest.TestCase):
    def test_returns_empty_runtime_when_tmux_missing(self) -> None:
        warnings: list[str] = []
        adapter = TmuxUIAdapter()
        runtime = adapter.create_runtime(
            config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
            workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
            run_id="run",
            console=Console(file=io.StringIO()),
            options={},
            warning_sink=warnings.append,
            which_fn=lambda executable: None,  # noqa: ARG005 - Required by callback or protocol signature.
        )
        self.assertEqual(runtime.observers, ())
        self.assertTrue(warnings)

    def test_returns_runtime_when_tmux_exists(self) -> None:
        class StubRuntime:
            def __init__(  # type: ignore[no-untyped-def]
                self,
                auto_close_session=True,
                tmux_executable="tmux",
                quiet_after_seconds=120.0,
                log_tail_lines=None,
                warning_sink=None,  # noqa: ARG002 - Required by callback or protocol signature.
            ):
                self.auto_close_session = auto_close_session
                self.tmux_executable = tmux_executable
                self.quiet_after_seconds = quiet_after_seconds
                self.log_tail_lines = log_tail_lines

        original_runtime_class = tmux_adapter_module.TmuxCompactRuntime
        tmux_adapter_module.TmuxCompactRuntime = StubRuntime  # type: ignore[assignment]

        adapter = TmuxUIAdapter()
        try:
            runtime = adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "auto_close_session": False,
                    "quiet_after_seconds": 180,
                    "log_tail_lines": 25,
                },
                which_fn=lambda executable: "/usr/bin/tmux",  # noqa: ARG005 - Required by callback or protocol signature.
            )
        finally:
            tmux_adapter_module.TmuxCompactRuntime = original_runtime_class  # type: ignore[assignment]
        self.assertEqual(len(runtime.observers), 1)
        self.assertTrue(runtime.suppress_progress_output)
        self.assertFalse(runtime.observers[0].auto_close_session)
        self.assertEqual(runtime.observers[0].quiet_after_seconds, 180.0)
        self.assertEqual(runtime.observers[0].log_tail_lines, 25)

    def test_canonicalize_options_marks_tmux_settings_observer_only(self) -> None:
        adapter = TmuxUIAdapter()
        config = adapter.canonicalize_options(
            implementation="tmux",
            resolved_identity="orchestrator_cli.adapters.ui.tmux:TmuxUIAdapter",
            options={"quiet_after_seconds": 180},
        )
        self.assertEqual(config.option_scopes["quiet_after_seconds"], "observer")
        self.assertEqual(config.options["quiet_after_seconds"], 180.0)

    def test_uses_default_tmux_liveness_options(self) -> None:
        class StubRuntime:
            def __init__(  # type: ignore[no-untyped-def]
                self,
                auto_close_session=True,
                tmux_executable="tmux",
                quiet_after_seconds=120.0,
                log_tail_lines=None,
                warning_sink=None,  # noqa: ARG002 - Required by callback or protocol signature.
            ):
                self.auto_close_session = auto_close_session
                self.tmux_executable = tmux_executable
                self.quiet_after_seconds = quiet_after_seconds
                self.log_tail_lines = log_tail_lines

        original_runtime_class = tmux_adapter_module.TmuxCompactRuntime
        tmux_adapter_module.TmuxCompactRuntime = StubRuntime  # type: ignore[assignment]

        adapter = TmuxUIAdapter()
        try:
            runtime = adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={},
                which_fn=lambda executable: "/usr/bin/tmux",  # noqa: ARG005 - Required by callback or protocol signature.
            )
        finally:
            tmux_adapter_module.TmuxCompactRuntime = original_runtime_class  # type: ignore[assignment]
        self.assertTrue(runtime.observers[0].auto_close_session)
        self.assertEqual(runtime.observers[0].tmux_executable, "tmux")
        self.assertEqual(runtime.observers[0].quiet_after_seconds, 120.0)
        self.assertIsNone(runtime.observers[0].log_tail_lines)

    def test_accepts_null_log_tail_lines(self) -> None:
        class StubRuntime:
            def __init__(  # type: ignore[no-untyped-def]
                self,
                auto_close_session=True,  # noqa: ARG002 - Required by callback or protocol signature.
                tmux_executable="tmux",  # noqa: ARG002 - Required by callback or protocol signature.
                quiet_after_seconds=120.0,  # noqa: ARG002 - Required by callback or protocol signature.
                log_tail_lines=None,
                warning_sink=None,  # noqa: ARG002 - Required by callback or protocol signature.
            ):
                self.log_tail_lines = log_tail_lines

        original_runtime_class = tmux_adapter_module.TmuxCompactRuntime
        tmux_adapter_module.TmuxCompactRuntime = StubRuntime  # type: ignore[assignment]

        adapter = TmuxUIAdapter()
        try:
            runtime = adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "log_tail_lines": None,
                },
                which_fn=lambda executable: "/usr/bin/tmux",  # noqa: ARG005 - Required by callback or protocol signature.
            )
        finally:
            tmux_adapter_module.TmuxCompactRuntime = original_runtime_class  # type: ignore[assignment]
        self.assertIsNone(runtime.observers[0].log_tail_lines)

    def test_rejects_invalid_quiet_after_seconds(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "quiet_after_seconds"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "quiet_after_seconds": 0,
                },
            )

    def test_rejects_invalid_quiet_after_seconds_type(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "quiet_after_seconds"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "quiet_after_seconds": True,
                },
            )

    def test_rejects_non_finite_quiet_after_seconds(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "quiet_after_seconds"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "quiet_after_seconds": float("nan"),
                },
            )

    def test_rejects_stale_after_seconds_alias(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "Unsupported tmux ui options"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "stale_after_seconds": 120,
                },
            )

    def test_rejects_invalid_log_tail_lines(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "log_tail_lines"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "log_tail_lines": 201,
                },
            )

    def test_rejects_invalid_log_tail_lines_type(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "log_tail_lines"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "log_tail_lines": "40",
                },
            )

    def test_rejects_invalid_auto_close_session_type(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "auto_close_session"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "auto_close_session": "false",
                },
            )

    def test_rejects_invalid_tmux_executable_type(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "tmux_executable"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "tmux_executable": 7,
                },
            )

    def test_rejects_empty_tmux_executable(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "tmux_executable"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "tmux_executable": "   ",
                },
            )

    def test_rejects_unsupported_tmux_option(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "Unsupported tmux ui options"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "unknown_option": True,
                },
            )

    def test_rejects_user_configured_which_option(self) -> None:
        adapter = TmuxUIAdapter()
        with self.assertRaisesRegex(ValueError, "Unsupported tmux ui options"):
            adapter.create_runtime(
                config=Config(version=CONFIG_SCHEMA_VERSION, agents={}),
                workflow_topology=WorkflowTopology(workflow_name="w", nodes=()),
                run_id="run",
                console=Console(file=io.StringIO()),
                options={
                    "which": lambda executable: "/usr/bin/tmux"  # noqa: ARG005 - Rejected user option value.
                },
                which_fn=lambda executable: "/usr/bin/tmux",  # noqa: ARG005 - Required by callback or protocol signature.
            )
