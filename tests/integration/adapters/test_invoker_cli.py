import unittest

from orchestrator_cli.adapters.invokers.cli import CliInvokerAdapter
from orchestrator_cli.core.config import AgentConfig, Config
from orchestrator_cli.core.versions import CONFIG_SCHEMA_VERSION


class CliInvokerAdapterTests(unittest.TestCase):
    def test_create_invoker_returns_default_invoker(self) -> None:
        adapter = CliInvokerAdapter()
        config = Config(
            version=CONFIG_SCHEMA_VERSION,
            agents={
                "alpha": AgentConfig(cli_cmd=["echo"], default_model="model"),
            },
        )
        invoker = adapter.create_invoker(config=config, options={})
        self.assertEqual(invoker.__class__.__name__, "DefaultAgentInvoker")

    def test_create_invoker_rejects_unknown_options(self) -> None:
        adapter = CliInvokerAdapter()
        config = Config(version=CONFIG_SCHEMA_VERSION, agents={})
        with self.assertRaisesRegex(ValueError, "does not support options"):
            adapter.create_invoker(config=config, options={"x": 1})
