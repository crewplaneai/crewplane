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
from crewplane.adapters.invokers.cli_invoker.capabilities import (
    CAPABILITIES,
    CODEX_MODEL_CAPACITY_MESSAGE,
)
from crewplane.architecture.contracts import (
    SUPPORTED_PROVIDER_KINDS,
    InvocationContext,
    ProviderKind,
)
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
        with self.assertRaisesRegex(ValueError, "options: \\{}"):
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
        gemini = build_cli_log_presentation(
            AgentConfig(cli_cmd=["gemini"], provider_kind="gemini")
        )
        kilo = build_cli_log_presentation(
            AgentConfig(cli_cmd=["kilo", "run"], provider_kind="kilo")
        )
        generic = build_cli_log_presentation(AgentConfig(cli_cmd=["echo"]))

        self.assertEqual((claude.format, claude.profile), ("json_object", "claude"))
        self.assertEqual((codex.format, codex.profile), ("json_lines", "codex"))
        self.assertEqual((gemini.format, gemini.profile), ("json_object", "gemini"))
        self.assertEqual((kilo.format, kilo.profile), ("json_lines", "kilo"))
        self.assertEqual((generic.format, generic.profile), ("plain", "generic"))

    def test_builtin_provider_capabilities_cover_supported_provider_kinds(self) -> None:
        self.assertEqual(set(CAPABILITIES), set(SUPPORTED_PROVIDER_KINDS))

    def test_machine_readable_provider_capabilities_supply_decoder_and_extractor(
        self,
    ) -> None:
        for provider in (ProviderKind.CLAUDE, ProviderKind.CODEX):
            capability = CAPABILITIES[provider]
            self.assertIsNotNone(capability.output_extractor)
            self.assertIsNotNone(capability.usage_decoder)
        for provider in (ProviderKind.GEMINI, ProviderKind.KILO):
            capability = CAPABILITIES[provider]
            self.assertIsNotNone(capability.output_extractor)
            self.assertIsNotNone(capability.usage_decoder)
        for provider in (ProviderKind.COPILOT, ProviderKind.GENERIC):
            capability = CAPABILITIES[provider]
            self.assertIsNone(capability.output_extractor)
            self.assertIsNone(capability.usage_decoder)

    def test_gemini_and_kilo_plans_enable_machine_readable_output(self) -> None:
        with patch.dict(os.environ, {"PATH": ""}):
            gemini_plan = build_cli_invocation_plan(
                AgentConfig(cli_cmd=["gemini"], provider_kind="gemini"),
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
            )
            kilo_plan = build_cli_invocation_plan(
                AgentConfig(cli_cmd=["kilo", "run"], provider_kind="kilo"),
                model="auto",
                prompt="prompt",
                output_file=Path("output.md"),
            )

        self.assertEqual(gemini_plan.cmd, ["gemini", "--output-format", "json"])
        self.assertEqual(
            kilo_plan.cmd,
            ["kilo", "run", "--model", "auto", "--format", "json"],
        )
        self.assertEqual(gemini_plan.structured_output_mode, "gemini_json")
        self.assertEqual(kilo_plan.structured_output_mode, "kilo_json")
        self.assertFalse(gemini_plan.supports_output_idle_timeout)
        self.assertTrue(kilo_plan.supports_output_idle_timeout)

    def test_only_gemini_disables_output_idle_timeout(self) -> None:
        self.assertFalse(CAPABILITIES[ProviderKind.GEMINI].supports_output_idle_timeout)
        for provider in set(SUPPORTED_PROVIDER_KINDS) - {ProviderKind.GEMINI}:
            with self.subTest(provider=provider):
                self.assertTrue(CAPABILITIES[provider].supports_output_idle_timeout)

    def test_codex_capability_owns_one_shot_capacity_retry(self) -> None:
        policy = CAPABILITIES[ProviderKind.CODEX].one_shot_failure_retry

        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertEqual(policy.output_contains, (CODEX_MODEL_CAPACITY_MESSAGE,))
        self.assertEqual(policy.wait_seconds, 5.0)
        self.assertIsNone(CAPABILITIES[ProviderKind.GENERIC].one_shot_failure_retry)

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

    def test_invocation_plan_preserves_missing_bare_cli_executable(
        self,
    ) -> None:
        with patch.dict(os.environ, {"PATH": ""}):
            plan = build_cli_invocation_plan(
                AgentConfig(cli_cmd=["missing-provider"]),
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
            )

        self.assertEqual(plan.cmd[0], "missing-provider")

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

    def test_codex_reasoning_request_builds_native_config_before_extra_args(
        self,
    ) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="codex",
            role="executor",
            requested_reasoning="xhigh",
        )
        plan = build_cli_invocation_plan(
            AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                extra_args=["--ephemeral"],
            ),
            model="gpt-5.5",
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )

        self.assertEqual(
            plan.cmd[1:8],
            [
                "exec",
                "--model",
                "gpt-5.5",
                "--config",
                'model_reasoning_effort="xhigh"',
                "--ephemeral",
                "--json",
            ],
        )
        self.assertIn("requested_reasoning: xhigh\n", plan.log_header.decode())

    def test_claude_reasoning_request_builds_native_effort(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="reviewer",
            requested_reasoning="high",
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
            plan = build_cli_invocation_plan(
                AgentConfig(cli_cmd=["claude"], provider_kind="claude"),
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

        self.assertEqual(plan.cmd[1:3], ["--effort", "high"])

    def test_reasoning_request_rejects_claude_settings_effort(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        configs = (
            AgentConfig(
                cli_cmd=[
                    "claude",
                    "--settings",
                    '{"effortLevel": "low"}',
                ],
                provider_kind="claude",
            ),
            AgentConfig(
                cli_cmd=["claude"],
                extra_args=['--settings={"effortLevel": "low"}'],
                provider_kind="claude",
            ),
        )

        with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
            for config in configs:
                with (
                    self.subTest(config=config),
                    self.assertRaisesRegex(ValueError, "--settings effortLevel"),
                ):
                    build_cli_invocation_plan(
                        config,
                        model=None,
                        prompt="prompt",
                        output_file=Path("output.md"),
                        invocation_context=context,
                    )

    def test_reasoning_request_rejects_claude_settings_environment(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        config = AgentConfig(
            cli_cmd=[
                "claude",
                "--settings",
                '{"env": {"CLAUDE_CODE_EFFORT_LEVEL": "low"}}',
            ],
            provider_kind="claude",
        )

        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}),
            self.assertRaisesRegex(ValueError, "CLAUDE_CODE_EFFORT_LEVEL"),
        ):
            build_cli_invocation_plan(
                config,
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

    def test_reasoning_request_allows_unrelated_claude_settings(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        config = AgentConfig(
            cli_cmd=[
                "claude",
                "--settings",
                (
                    '{"permissions": {"allow": ["Read"]}, '
                    '"effortLevel": "", '
                    '"env": {"CLAUDE_CODE_EFFORT_LEVEL": ""}}'
                ),
            ],
            provider_kind="claude",
        )

        with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
            plan = build_cli_invocation_plan(
                config,
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

        self.assertIn("--effort", plan.cmd)

    def test_reasoning_request_checks_relative_claude_settings_file(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            working_directory = Path(tmp_dir)
            (working_directory / "claude-settings.json").write_text(
                '{"effortLevel": "low"}',
                encoding="utf-8",
            )
            config = AgentConfig(
                cli_cmd=["claude", "--settings", "claude-settings.json"],
                provider_kind="claude",
            )

            with (
                patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}),
                self.assertRaisesRegex(ValueError, "--settings effortLevel"),
            ):
                build_cli_invocation_plan(
                    config,
                    model=None,
                    prompt="prompt",
                    output_file=working_directory / "output.md",
                    invocation_context=context,
                    working_directory=working_directory,
                )

    def test_reasoning_request_fails_closed_for_unreadable_claude_settings(
        self,
    ) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        config = AgentConfig(
            cli_cmd=["claude", "--settings", "missing-settings.json"],
            provider_kind="claude",
        )

        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}),
            self.assertRaisesRegex(ValueError, "Cannot validate"),
        ):
            build_cli_invocation_plan(
                config,
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

    def test_reasoning_request_rejects_direct_codex_config_conflict(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="codex",
            role="executor",
            requested_reasoning="high",
        )
        conflict_forms = (
            ["--config", 'model_reasoning_effort="low"'],
            ['--config=model_reasoning_effort="low"'],
            ["-c", 'model_reasoning_effort = "low"'],
            ['-c=model_reasoning_effort="low"'],
            ['-cmodel_reasoning_effort="low"'],
        )

        for extra_args in conflict_forms:
            with (
                self.subTest(extra_args=extra_args),
                self.assertRaisesRegex(ValueError, "model_reasoning_effort"),
            ):
                build_cli_invocation_plan(
                    AgentConfig(
                        cli_cmd=["codex", "exec"],
                        provider_kind="codex",
                        extra_args=extra_args,
                    ),
                    model="gpt-5.5",
                    prompt="prompt",
                    output_file=Path("output.md"),
                    invocation_context=context,
                )

    def test_codex_reasoning_conflict_scan_respects_extra_args_terminator(
        self,
    ) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="codex",
            role="executor",
            requested_reasoning="high",
        )

        plan = build_cli_invocation_plan(
            AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                extra_args=["--", 'model_reasoning_effort="low"'],
            ),
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )

        self.assertEqual(
            plan.cmd[2:6],
            [
                "--config",
                'model_reasoning_effort="high"',
                "--",
                'model_reasoning_effort="low"',
            ],
        )

    def test_codex_reasoning_allows_env_prefix_provider_like_tokens(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="codex",
            role="executor",
            requested_reasoning="high",
        )

        plan = build_cli_invocation_plan(
            AgentConfig(
                cli_cmd=["env", "-u", "--config", "codex"],
                provider_kind="codex",
            ),
            model=None,
            prompt="prompt",
            output_file=Path("output.md"),
            invocation_context=context,
        )

        command_index = plan.cmd.index("codex")
        self.assertEqual(
            plan.cmd[command_index + 1 : command_index + 3],
            ["--config", 'model_reasoning_effort="high"'],
        )

    def test_reasoning_request_rejects_cli_command_option_terminator(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="codex",
            role="executor",
            requested_reasoning="high",
        )

        with self.assertRaisesRegex(ValueError, "option terminator"):
            build_cli_invocation_plan(
                AgentConfig(
                    cli_cmd=["codex", "exec", "--"],
                    provider_kind="codex",
                ),
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

    def test_reasoning_request_rejects_unsupported_provider_kind(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="generic",
            role="executor",
            requested_reasoning="high",
        )

        with self.assertRaisesRegex(ValueError, "provider_kind 'codex' or 'claude'"):
            build_cli_invocation_plan(
                AgentConfig(cli_cmd=["provider"], provider_kind="generic"),
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

    def test_reasoning_request_rejects_claude_environment_conflict(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "low"}),
            self.assertRaisesRegex(ValueError, "CLAUDE_CODE_EFFORT_LEVEL"),
        ):
            build_cli_invocation_plan(
                AgentConfig(cli_cmd=["claude"], provider_kind="claude"),
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

    def test_reasoning_request_rejects_claude_cli_environment_assignment(
        self,
    ) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        config = AgentConfig(
            cli_cmd=["env", "CLAUDE_CODE_EFFORT_LEVEL=low", "claude"],
            provider_kind="claude",
        )

        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}),
            self.assertRaisesRegex(ValueError, "CLAUDE_CODE_EFFORT_LEVEL"),
        ):
            build_cli_invocation_plan(
                config,
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

    def test_reasoning_request_allows_blank_claude_cli_environment_assignment(
        self,
    ) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        config = AgentConfig(
            cli_cmd=["env", "CLAUDE_CODE_EFFORT_LEVEL=", "claude"],
            provider_kind="claude",
        )

        with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "low"}):
            plan = build_cli_invocation_plan(
                config,
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

        self.assertIn("--effort", plan.cmd)

    def test_reasoning_request_rejects_path_qualified_env_assignment(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        config = AgentConfig(
            cli_cmd=[
                "/usr/bin/env",
                "-i",
                "CLAUDE_CODE_EFFORT_LEVEL=low",
                "claude",
            ],
            provider_kind="claude",
        )

        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}),
            self.assertRaisesRegex(ValueError, "CLAUDE_CODE_EFFORT_LEVEL"),
        ):
            build_cli_invocation_plan(
                config,
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

    def test_reasoning_request_allows_assignment_tokens_after_env_command(
        self,
    ) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        assignment = "CLAUDE_CODE_EFFORT_LEVEL=low"
        configs = (
            AgentConfig(
                cli_cmd=["env", "claude", assignment],
                provider_kind="claude",
            ),
            AgentConfig(
                cli_cmd=["claude"],
                extra_args=[assignment],
                provider_kind="claude",
            ),
        )

        with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
            for config in configs:
                with self.subTest(config=config):
                    plan = build_cli_invocation_plan(
                        config,
                        model=None,
                        prompt="prompt",
                        output_file=Path("output.md"),
                        invocation_context=context,
                    )
                    self.assertIn(assignment, plan.cmd)
                    self.assertIn("--effort", plan.cmd)

    def test_reasoning_request_allows_env_to_remove_inherited_effort(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        configs = (
            AgentConfig(
                cli_cmd=["env", "-i", "claude"],
                provider_kind="claude",
            ),
            AgentConfig(
                cli_cmd=["env", "-u", "CLAUDE_CODE_EFFORT_LEVEL", "claude"],
                provider_kind="claude",
            ),
            AgentConfig(
                cli_cmd=[
                    "env",
                    "--unset=CLAUDE_CODE_EFFORT_LEVEL",
                    "claude",
                ],
                provider_kind="claude",
            ),
        )

        with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "low"}):
            for config in configs:
                with self.subTest(config=config):
                    plan = build_cli_invocation_plan(
                        config,
                        model=None,
                        prompt="prompt",
                        output_file=Path("output.md"),
                        invocation_context=context,
                    )
                    self.assertIn("--effort", plan.cmd)

    def test_reasoning_request_rejects_clustered_env_assignment(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        config = AgentConfig(
            cli_cmd=[
                "env",
                "-iv",
                "CLAUDE_CODE_EFFORT_LEVEL=low",
                "claude",
            ],
            provider_kind="claude",
        )

        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}),
            self.assertRaisesRegex(ValueError, "CLAUDE_CODE_EFFORT_LEVEL"),
        ):
            build_cli_invocation_plan(
                config,
                model=None,
                prompt="prompt",
                output_file=Path("output.md"),
                invocation_context=context,
            )

    def test_reasoning_request_rejects_env_split_string(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        split_value = "CLAUDE_CODE_EFFORT_LEVEL=low claude"
        cli_commands = (
            ["env", "-S", split_value],
            ["env", f"-S{split_value}"],
            ["env", "--split-string", split_value],
            ["env", f"--split-string={split_value}"],
        )

        with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
            for cli_cmd in cli_commands:
                with (
                    self.subTest(cli_cmd=cli_cmd),
                    self.assertRaisesRegex(ValueError, "cannot be combined"),
                ):
                    build_cli_invocation_plan(
                        AgentConfig(cli_cmd=cli_cmd, provider_kind="claude"),
                        model=None,
                        prompt="prompt",
                        output_file=Path("output.md"),
                        invocation_context=context,
                    )

    def test_reasoning_request_rejects_env_working_directory_change(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        cli_commands = (
            ["env", "--chdir", "subdir", "claude"],
            ["env", "--chdir=subdir", "claude"],
            ["env", "-C", "subdir", "claude"],
            ["env", "-Csubdir", "claude"],
        )

        with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
            for cli_cmd in cli_commands:
                with (
                    self.subTest(cli_cmd=cli_cmd),
                    self.assertRaisesRegex(ValueError, "cannot be combined"),
                ):
                    build_cli_invocation_plan(
                        AgentConfig(cli_cmd=cli_cmd, provider_kind="claude"),
                        model=None,
                        prompt="prompt",
                        output_file=Path("output.md"),
                        invocation_context=context,
                    )

    def test_reasoning_request_allows_env_prefix_provider_like_tokens(self) -> None:
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="claude",
            role="executor",
            requested_reasoning="high",
        )
        cli_commands = (
            ["env", "--", "claude"],
            ["env", "-u", "--effort", "claude"],
            ["env", "--unset", "--effort", "claude"],
        )

        with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": ""}):
            for cli_cmd in cli_commands:
                with self.subTest(cli_cmd=cli_cmd):
                    plan = build_cli_invocation_plan(
                        AgentConfig(cli_cmd=cli_cmd, provider_kind="claude"),
                        model=None,
                        prompt="prompt",
                        output_file=Path("output.md"),
                        invocation_context=context,
                    )
                    command_index = plan.cmd.index("claude")
                    self.assertEqual(
                        plan.cmd[command_index + 1 : command_index + 3],
                        ["--effort", "high"],
                    )
