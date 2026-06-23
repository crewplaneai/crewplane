import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.adapters.invokers.cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
)
from crewplane.adapters.invokers.cli_invoker.capabilities import CAPABILITIES
from crewplane.architecture.contracts import SUPPORTED_PROVIDER_KINDS
from crewplane.core.config import AgentConfig, Config
from crewplane.version import SCHEMA_VERSION


class CliInvokerAdapterTests(unittest.TestCase):
    def test_create_invoker_returns_default_invoker(self) -> None:
        adapter = CliInvokerAdapter()
        config = Config(
            version=SCHEMA_VERSION,
            agents={
                "alpha": AgentConfig(cli_cmd=["echo"], default_model="model"),
            },
        )
        invoker = adapter.create_invoker(config=config, options={})
        self.assertEqual(invoker.__class__.__name__, "PlannedAgentInvoker")

    def test_create_invoker_rejects_unknown_options(self) -> None:
        adapter = CliInvokerAdapter()
        config = Config(version=SCHEMA_VERSION, agents={})
        with self.assertRaisesRegex(ValueError, "does not support options"):
            adapter.create_invoker(config=config, options={"x": 1})

    def test_workspace_capabilities_declare_runtime_command_runner(self) -> None:
        adapter = CliInvokerAdapter()

        capabilities = adapter.workspace_capabilities().as_dict()["workspace"]

        self.assertEqual(capabilities["supported"], True)
        self.assertEqual(capabilities["launch_mode"], "runtime_command_runner")
        self.assertEqual(capabilities["honors_cwd"], True)
        self.assertEqual(capabilities["controlled_child_environment"], True)

    def test_builtin_provider_log_presentation_descriptors(self) -> None:
        claude = build_cli_log_presentation(
            AgentConfig(cli_cmd=["claude"], provider_kind="claude")
        )
        codex = build_cli_log_presentation(
            AgentConfig(cli_cmd=["codex"], provider_kind="codex")
        )
        generic = build_cli_log_presentation(AgentConfig(cli_cmd=["echo"]))

        self.assertEqual((claude.format, claude.profile), ("json_object", "claude"))
        self.assertEqual((codex.format, codex.profile), ("json_lines", "codex"))
        self.assertEqual((generic.format, generic.profile), ("plain", "generic"))

    def test_builtin_provider_capabilities_cover_supported_provider_kinds(self) -> None:
        self.assertEqual(set(CAPABILITIES), set(SUPPORTED_PROVIDER_KINDS))

    def test_invocation_plan_resolves_cli_executable_before_workspace_cwd(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tool_dir = Path(tmp_dir) / "tools"
            tool_dir.mkdir()
            executable = tool_dir / "provider"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            expected_executable = executable.resolve(strict=True).as_posix()
            workspace_dir = Path(tmp_dir) / "workspace"
            workspace_dir.mkdir()
            (workspace_dir / "provider").write_text(
                "#!/bin/sh\nexit 99\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"PATH": tool_dir.as_posix()}):
                plan = build_cli_invocation_plan(
                    AgentConfig(cli_cmd=["provider"]),
                    model=None,
                    prompt="prompt",
                    output_file=workspace_dir / "output.md",
                )

        self.assertEqual(plan.cmd[0], expected_executable)

    def test_invocation_plan_preserves_relative_path_cli_executable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tool_dir = Path(tmp_dir) / "tools"
            tool_dir.mkdir()
            executable = tool_dir / "provider"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            relative_executable = os.path.relpath(executable, Path.cwd())
            config = AgentConfig(cli_cmd=["echo"]).model_copy(
                update={"cli_cmd": [relative_executable]}
            )

            plan = build_cli_invocation_plan(
                config,
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
            )

        self.assertEqual(plan.cmd[0], relative_executable)
