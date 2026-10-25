"""Build a real plugin invoker for local HTTP-backed workflow acceptance."""

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.core.config import AgentConfig
from tests.helpers.cli_provider import OLLAMA_PACKAGE, provider_config
from tests.helpers.ollama_agent import agent_executable


def configured_agent(root, endpoint):
    agent = AgentConfig(
        cli_cmd=[str(agent_executable(root))],
        provider_kind="ollama",
        default_model="test-model",
        invocation_timeout_seconds=30,
        environment={
            "OLLAMA_HOST": endpoint,
            "PYTHONPATH": str(OLLAMA_PACKAGE / "src"),
        },
    )
    options = {
        "providers": {
            "ollama": {
                "implementation": "crewplane_ollama:OLLAMA",
                "distribution": "crewplane-ollama",
            }
        }
    }
    config = provider_config(agent, options)
    adapter = CliInvokerAdapter()
    canonical = adapter.canonicalize_options("cli", "cli", options).options
    return agent, config, adapter.create_invoker(config, canonical)
