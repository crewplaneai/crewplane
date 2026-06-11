import unittest

from orchestrator_cli.adapters.invokers.cli import CliInvokerAdapter
from orchestrator_cli.adapters.invokers.cli_invoker import build_cli_log_presentation
from orchestrator_cli.core.config import AgentConfig, Config
from orchestrator_cli.version import SCHEMA_VERSION


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
