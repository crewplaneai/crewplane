import pytest
from pydantic import ValidationError

from crewplane.core.config import AgentConfig
from crewplane.core.preflight.runtime_config import RuntimeAgentConfigSnapshot


@pytest.mark.parametrize("model", [AgentConfig, RuntimeAgentConfigSnapshot])
@pytest.mark.parametrize("identifier", ["ollama", "local-text_v2", "a0", "generic"])
def test_provider_identifiers_are_extensible(model, identifier):
    config = model(cli_cmd=["provider"], provider_kind=identifier)
    assert config.provider_kind == identifier
    assert config.model_dump(mode="json")["provider_kind"] == identifier


@pytest.mark.parametrize("model", [AgentConfig, RuntimeAgentConfigSnapshot])
@pytest.mark.parametrize(
    "identifier", ["", " Ollama", "ollama ", "OLlama", "é", "2a", "a.b", "a\n", None, 3]
)
def test_provider_identifiers_reject_malformed_values(model, identifier):
    with pytest.raises(ValidationError, match="provider_kind"):
        model(cli_cmd=["provider"], provider_kind=identifier)


@pytest.mark.parametrize("model", [AgentConfig, RuntimeAgentConfigSnapshot])
def test_default_provider_remains_generic(model):
    assert model(cli_cmd=["provider"]).provider_kind == "generic"
