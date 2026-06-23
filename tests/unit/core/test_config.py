import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from crewplane.architecture.contracts import SUPPORTED_PROVIDER_KIND_VALUES
from crewplane.core.config import (
    DEFAULT_INVOCATION_IDLE_TIMEOUT_SECONDS,
    DEFAULT_INVOCATION_TIMEOUT_SECONDS,
    DEFAULT_MOCK_INVOKER_OBSERVATION_DELAY_SECONDS,
    AgentConfig,
    Config,
    Settings,
    load_config,
)
from crewplane.core.token_budget import TokenBudgetOverride, resolve_token_budget
from crewplane.version import SCHEMA_VERSION


class ConfigTests(unittest.TestCase):
    def test_agent_config_rejects_empty_command(self):
        with self.assertRaisesRegex(
            ValueError, "cli_cmd must contain at least one token"
        ):
            AgentConfig(cli_cmd=[], default_model="test-model")

    def test_agent_config_rejects_blank_command_tokens(self) -> None:
        with self.assertRaisesRegex(ValueError, "blank tokens"):
            AgentConfig(cli_cmd=["echo", "   "], default_model="test-model")
        with self.assertRaisesRegex(ValueError, "command argument"):
            AgentConfig(cli_cmd=["echo"], model_arg=" ")
        with self.assertRaisesRegex(ValueError, "extra_args"):
            AgentConfig(cli_cmd=["echo"], extra_args=["--flag", ""])

    def test_agent_config_allows_relative_cli_executable_path(self) -> None:
        config = AgentConfig(cli_cmd=["./bin/provider"])

        self.assertEqual(config.cli_cmd, ["./bin/provider"])

    def test_agent_config_allows_omitted_default_model(self) -> None:
        config = AgentConfig(cli_cmd=["echo"])
        self.assertIsNone(config.default_model)

    def test_agent_config_defaults_to_disabled_wall_clock_timeout(
        self,
    ) -> None:
        config = AgentConfig(cli_cmd=["echo"])

        self.assertIsNone(DEFAULT_INVOCATION_TIMEOUT_SECONDS)
        self.assertIsNone(config.invocation_timeout_seconds)
        self.assertEqual(
            config.invocation_idle_timeout_seconds,
            DEFAULT_INVOCATION_IDLE_TIMEOUT_SECONDS,
        )

    def test_agent_config_allows_finite_invocation_timeout(self) -> None:
        config = AgentConfig(
            cli_cmd=["echo"],
            invocation_timeout_seconds=7200,
        )

        self.assertEqual(config.invocation_timeout_seconds, 7200)

    def test_agent_config_allows_disabled_invocation_timeout(self) -> None:
        config = AgentConfig(
            cli_cmd=["echo"],
            invocation_timeout_seconds=None,
            invocation_idle_timeout_seconds=None,
        )

        self.assertIsNone(config.invocation_timeout_seconds)
        self.assertIsNone(config.invocation_idle_timeout_seconds)

    def test_agent_config_rejects_non_positive_invocation_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "invocation_timeout_seconds"):
            AgentConfig(cli_cmd=["echo"], invocation_timeout_seconds=0)
        with self.assertRaisesRegex(ValueError, "invocation_idle_timeout_seconds"):
            AgentConfig(cli_cmd=["echo"], invocation_idle_timeout_seconds=0)

    def test_get_command_returns_copy(self):
        config = AgentConfig(cli_cmd=["echo"], default_model="test-model")
        cmd = config.get_command()
        cmd.append("mutated")
        self.assertEqual(config.cli_cmd, ["echo"])

    def test_agent_config_accepts_every_supported_provider_kind(self) -> None:
        for provider_kind in SUPPORTED_PROVIDER_KIND_VALUES:
            with self.subTest(provider_kind=provider_kind):
                config = AgentConfig(
                    cli_cmd=["echo"],
                    default_model="test-model",
                    provider_kind=provider_kind,
                )

                self.assertEqual(config.provider_kind, provider_kind)
                self.assertEqual(
                    config.model_dump(mode="json")["provider_kind"],
                    provider_kind,
                )

    def test_agent_config_rejects_mixed_case_provider_kind(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "provider_kind"):
            AgentConfig(
                cli_cmd=["echo"],
                default_model="test-model",
                provider_kind=" Gemini ",
            )

    def test_agent_config_rejects_invalid_provider_kind(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider_kind"):
            AgentConfig(
                cli_cmd=["echo"],
                default_model="test-model",
                provider_kind="unsupported",
            )

    def test_agent_config_defaults_prompt_transport_to_stdin(self) -> None:
        config = AgentConfig(cli_cmd=["echo"])

        self.assertEqual(config.prompt_transport, "stdin")
        self.assertIsNone(config.prompt_transport_arg)

    def test_agent_config_requires_argv_prompt_transport_arg(self) -> None:
        with self.assertRaisesRegex(ValueError, "prompt_transport_arg"):
            AgentConfig(cli_cmd=["echo"], prompt_transport="argv")

    def test_agent_config_allows_explicit_argv_prompt_transport(self) -> None:
        config = AgentConfig(
            cli_cmd=["echo"],
            prompt_transport="argv",
            prompt_transport_arg="--prompt",
        )

        self.assertEqual(config.prompt_transport, "argv")
        self.assertEqual(config.prompt_transport_arg, "--prompt")

    def test_agent_config_rejects_negative_quota_sleep_floor(self) -> None:
        with self.assertRaisesRegex(ValueError, "quota_reset_sleep_floor_seconds"):
            AgentConfig(
                cli_cmd=["echo"],
                default_model="test-model",
                quota_reset_sleep_floor_seconds=-1,
            )

    def test_agent_config_accepts_optional_pricing_buckets(self) -> None:
        config = AgentConfig(
            cli_cmd=["echo"],
            default_model="test-model",
            pricing={"input": 1.25, "output": 5.5},
        )
        self.assertEqual(config.pricing.input, 1.25)
        self.assertEqual(config.pricing.output, 5.5)

    def test_agent_config_rejects_negative_pricing_buckets(self) -> None:
        with self.assertRaisesRegex(ValueError, "input"):
            AgentConfig(
                cli_cmd=["echo"],
                default_model="test-model",
                pricing={"input": -0.1},
            )
        with self.assertRaisesRegex(ValueError, "output"):
            AgentConfig(
                cli_cmd=["echo"],
                default_model="test-model",
                pricing={"output": -0.1},
            )

    def test_agent_config_rejects_total_pricing_with_bucket_rates(self) -> None:
        with self.assertRaisesRegex(ValueError, "pricing.total"):
            AgentConfig(
                cli_cmd=["echo"],
                default_model="test-model",
                pricing={"input": 1.25, "total": 5.5},
            )

    def test_load_config_accepts_pricing_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.yml"
            path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "",
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                        '    default_model: "x"',
                        "    pricing:",
                        "      input: 1.25",
                        "      output: 5.5",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(path)

        self.assertEqual(
            config.agents["alpha"].pricing.input,
            1.25,
        )
        self.assertEqual(
            config.agents["alpha"].pricing.output,
            5.5,
        )

    def test_config_rejects_blank_agent_names(self) -> None:
        with self.assertRaisesRegex(ValidationError, "agent name"):
            Config(
                version=SCHEMA_VERSION,
                agents={"   ": AgentConfig(cli_cmd=["echo"])},
            )

    def test_config_normalizes_agent_names_and_rejects_trimmed_duplicates(
        self,
    ) -> None:
        config = Config(
            version=SCHEMA_VERSION,
            agents={" alpha ": AgentConfig(cli_cmd=["echo"])},
        )
        self.assertIn("alpha", config.agents)
        with self.assertRaisesRegex(ValidationError, "Duplicate agent name"):
            Config(
                version=SCHEMA_VERSION,
                agents={
                    " alpha ": AgentConfig(cli_cmd=["echo"]),
                    "alpha": AgentConfig(cli_cmd=["echo"]),
                },
            )

    def test_workspace_settings_default_disabled(self) -> None:
        settings = Settings()

        self.assertFalse(settings.workspace.enabled)
        self.assertIsNone(settings.workspace.cache_root)
        self.assertTrue(settings.workspace.cleanup_on_success)
        self.assertEqual(settings.workspace.worktree_contract, "blob_exact")
        self.assertEqual(settings.workspace.clean_start, "strict")
        self.assertFalse(settings.workspace.identity.include_cache_root)

    def test_workspace_settings_reject_string_booleans(self) -> None:
        for workspace in (
            {"enabled": "true"},
            {"cleanup_on_success": "false"},
            {"identity": {"include_cache_root": "yes"}},
        ):
            with self.subTest(workspace=workspace), self.assertRaises(ValidationError):
                Settings(workspace=workspace)

    def test_workspace_settings_rejects_invalid_contract_modes(self) -> None:
        with self.assertRaisesRegex(ValidationError, "worktree_contract"):
            Settings(workspace={"worktree_contract": "blob_exact_v1"})
        with self.assertRaisesRegex(ValidationError, "worktree_contract"):
            Settings(workspace={"worktree_contract": "text_normalized"})

    def test_workspace_settings_rejects_invalid_clean_start(self) -> None:
        with self.assertRaisesRegex(ValidationError, "clean_start"):
            Settings(workspace={"clean_start": "dirty_ok"})

    def test_workspace_setup_profiles_require_argv_command_lists(self) -> None:
        with self.assertRaisesRegex(ValidationError, "at least one command"):
            Settings(workspace={"setup_profiles": {"bootstrap": {"run": []}}})
        with self.assertRaisesRegex(ValidationError, "at least one command"):
            Settings(workspace={"setup_profiles": {"bootstrap": {}}})
        with self.assertRaisesRegex(ValidationError, "not shell strings"):
            Settings(workspace={"setup_profiles": {"bootstrap": {"run": ["uv sync"]}}})
        with self.assertRaisesRegex(ValidationError, "commands cannot be empty"):
            Settings(workspace={"setup_profiles": {"bootstrap": {"run": [[]]}}})
        with self.assertRaisesRegex(ValidationError, "blank tokens"):
            Settings(
                workspace={"setup_profiles": {"bootstrap": {"run": [["uv", " "]]}}}
            )

    def test_workspace_materialization_and_disk_guardrails_are_validated(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            Settings(workspace={"max_concurrent_materializations": 0})
        with self.assertRaises(ValidationError):
            Settings(workspace={"disk": {"warn_free_bytes": -1}})
        with self.assertRaisesRegex(ValidationError, "cannot exceed warn_free_bytes"):
            Settings(
                workspace={
                    "disk": {
                        "warn_free_bytes": 100,
                        "fail_free_bytes": 200,
                    }
                }
            )

    def test_workspace_settings_rejects_relative_cache_root_when_enabled(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "settings.workspace.cache_root must be absolute",
        ):
            Settings(
                workspace={
                    "enabled": True,
                    "cache_root": ".crewplane/workspaces",
                }
            )

    def test_workspace_settings_reports_unresolved_cache_root_user(self) -> None:
        with self.assertRaisesRegex(ValidationError, "could not expand user home"):
            Settings(
                workspace={
                    "enabled": True,
                    "cache_root": "~crewplane_missing_user_for_tests/cache",
                }
            )

    def test_settings_rejects_unknown_keys(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            Settings.model_validate({"default_workspace": ".crewplane/workspaces"})

    def test_load_config_rejects_unknown_settings_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.yml"
            path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "",
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                        "",
                        "settings:",
                        '  default_workspace: ".crewplane/workspaces"',
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValidationError,
                "Extra inputs are not permitted",
            ):
                load_config(path)

    def test_load_config_preserves_mock_observation_delay_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.yml"
            path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "",
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                        '    default_model: "x"',
                        "settings:",
                        "  integrations:",
                        "    invoker:",
                        '      implementation: "mock"',
                        "      options:",
                        (
                            "        observation_delay_seconds: "
                            f"{DEFAULT_MOCK_INVOKER_OBSERVATION_DELAY_SECONDS}"
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(path)

        assert config.settings is not None
        self.assertEqual(
            config.settings.integrations.invoker.options["observation_delay_seconds"],
            DEFAULT_MOCK_INVOKER_OBSERVATION_DELAY_SECONDS,
        )

    def test_load_config_rejects_non_object_yaml(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.yml"
            path.write_text("- not-an-object", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "YAML object"):
                load_config(path)

    def test_load_config_rejects_duplicate_top_level_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.yml"
            path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        'version: "{SCHEMA_VERSION}"',
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate YAML key 'version'"):
                load_config(path)

    def test_load_config_rejects_nested_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.yml"
            path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                        '    cli_cmd: ["python3"]',
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate YAML key 'cli_cmd'"):
                load_config(path)

    def test_load_config_rejects_unknown_top_level_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.yml"
            path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                        "settngs:",
                        "  log_level: debug",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "settngs"):
                load_config(path)

    def test_load_config_rejects_unsupported_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.yml"
            path.write_text(
                "\n".join(
                    [
                        'version: "9.9"',
                        "",
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                        '    default_model: "x"',
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as exc_info:
                load_config(path)
            message = str(exc_info.exception)
            self.assertIn(f"Expected '{SCHEMA_VERSION}'", message)
            self.assertIn("crewplane init", message)

    def test_settings_default_integrations(self) -> None:
        settings = Settings()
        self.assertEqual(settings.integrations.invoker.implementation, "cli")
        self.assertEqual(settings.integrations.ui.implementation, "tmux")
        self.assertEqual(settings.integrations.artifacts.implementation, "filesystem")
        self.assertEqual(settings.sequential_consensus_on_exhaustion, "continue")
        self.assertEqual(settings.token_budget.warn_threshold_chars, 50000)
        self.assertIsNone(settings.token_budget.fail_threshold_chars)
        self.assertEqual(
            settings.integrations.ui.options.get("auto_close_session"),
            True,
        )
        self.assertEqual(
            settings.integrations.ui.options.get("quiet_after_seconds"),
            120.0,
        )
        self.assertIsNone(settings.integrations.ui.options.get("log_tail_lines"))
        self.assertIsNone(settings.max_concurrent_nodes)
        self.assertIsNone(settings.max_parallel_invocations)
        self.assertEqual(settings.max_audit_rounds, 5)

    def test_settings_rejects_invalid_sequential_consensus_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "sequential_consensus_on_exhaustion"):
            Settings(sequential_consensus_on_exhaustion="sometimes")

    def test_settings_rejects_non_positive_max_audit_rounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_audit_rounds"):
            Settings(max_audit_rounds=0)

    def test_settings_rejects_mixed_case_sequential_consensus_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be lower-case"):
            Settings(sequential_consensus_on_exhaustion="Continue")

    def test_settings_accepts_token_budget_overrides(self) -> None:
        settings = Settings(
            token_budget={
                "warn_threshold_chars": 1000,
                "fail_threshold_chars": 2000,
            }
        )
        self.assertEqual(settings.token_budget.warn_threshold_chars, 1000)
        self.assertEqual(settings.token_budget.fail_threshold_chars, 2000)

    def test_settings_rejects_invalid_token_budget_threshold_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "fail_threshold_chars"):
            Settings(
                token_budget={
                    "warn_threshold_chars": 2000,
                    "fail_threshold_chars": 1000,
                }
            )

    def test_settings_rejects_non_positive_token_budget_thresholds(self) -> None:
        with self.assertRaisesRegex(ValueError, "warn_threshold_chars"):
            Settings(token_budget={"warn_threshold_chars": 0})
        with self.assertRaisesRegex(ValueError, "fail_threshold_chars"):
            Settings(token_budget={"fail_threshold_chars": 0})

    def test_resolve_token_budget_applies_field_by_field_node_overrides(self) -> None:
        settings = Settings(
            token_budget={
                "warn_threshold_chars": 1000,
                "fail_threshold_chars": 4000,
            }
        )
        resolved = resolve_token_budget(
            settings.token_budget,
            TokenBudgetOverride(warn_threshold_chars=1500),
        )

        self.assertEqual(resolved.warn_threshold_chars, 1500)
        self.assertEqual(resolved.fail_threshold_chars, 4000)

    def test_resolve_token_budget_allows_explicit_null_override(self) -> None:
        settings = Settings(
            token_budget={
                "warn_threshold_chars": 1000,
                "fail_threshold_chars": 4000,
            }
        )
        resolved = resolve_token_budget(
            settings.token_budget,
            TokenBudgetOverride(fail_threshold_chars=None),
        )

        self.assertEqual(resolved.warn_threshold_chars, 1000)
        self.assertIsNone(resolved.fail_threshold_chars)

    def test_settings_rejects_empty_implementation(self) -> None:
        with self.assertRaisesRegex(ValueError, "implementation"):
            Settings(
                integrations={
                    "ui": {
                        "implementation": "   ",
                    }
                }
            )

    def test_settings_rejects_legacy_live_tmux_auto_close_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "live_tmux_auto_close"):
            Settings(live_tmux_auto_close=False)

    def test_settings_rejects_legacy_log_cli_output_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "log_cli_output"):
            Settings(log_cli_output=True)

    def test_settings_accepts_integration_overrides(self) -> None:
        settings = Settings(
            integrations={
                "invoker": {
                    "implementation": "cli",
                    "options": {},
                },
                "ui": {
                    "implementation": "none",
                    "options": {},
                },
                "artifacts": {
                    "implementation": "filesystem",
                    "options": {
                        "log_cli_output": False,
                        "allowed_template_paths": ["/tmp"],
                    },
                },
            }
        )
        self.assertEqual(settings.integrations.ui.implementation, "none")
        self.assertEqual(
            settings.integrations.artifacts.options["log_cli_output"],
            False,
        )

    def test_load_config_preserves_null_tmux_log_tail_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.yml"
            path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "",
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                        '    default_model: "x"',
                        "settings:",
                        "  integrations:",
                        "    ui:",
                        '      implementation: "tmux"',
                        "      options:",
                        "        log_tail_lines: null",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(path)

        self.assertIsNotNone(config.settings)
        self.assertIsNone(config.settings.integrations.ui.options["log_tail_lines"])

    def test_settings_rejects_invalid_concurrency_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "greater than or equal to 1"):
            Settings(max_concurrent_nodes=0)
        with self.assertRaisesRegex(ValueError, "greater than or equal to 1"):
            Settings(max_parallel_invocations=0)
