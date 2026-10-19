import pytest
from pydantic import ValidationError

from crewplane.core import agent_environment
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.runtime_config import (
    RuntimeAgentConfigSnapshot,
    agent_config_input_payload,
    runtime_agent_signature_payload,
)


@pytest.mark.parametrize(
    "environment",
    [{"": "x"}, {"A=B": "x"}, {"A\0B": "x"}, {"A": "x\0"}, {"A": 3}],
)
def test_agent_environment_rejects_invalid_process_values(environment):
    with pytest.raises(ValidationError):
        AgentConfig(cli_cmd=["provider"], environment=environment)


def test_environment_survives_snapshot_and_changes_execution_identity():
    agent = AgentConfig(
        cli_cmd=["provider"], environment={"OLLAMA_HOST": "http://localhost:11434"}
    )
    first = RuntimeAgentConfigSnapshot.model_validate(agent_config_input_payload(agent))
    second = first.model_copy(
        update={"environment": {"OLLAMA_HOST": "http://host:11434"}}
    )
    assert first.environment == agent.environment
    assert runtime_agent_signature_payload(
        "local", first, None
    ) != runtime_agent_signature_payload("local", second, None)


def test_windows_environment_overrides_are_case_insensitive(monkeypatch):
    monkeypatch.setattr(agent_environment, "is_native_windows", lambda: True)
    inherited = {"Path": "old", "Ollama_Host": "old"}
    assert agent_environment.merge_agent_environment(
        inherited, {"PATH": "new", "ollama_host": "new"}
    ) == {"PATH": "new", "OLLAMA_HOST": "new"}
    assert inherited == {"Path": "old", "Ollama_Host": "old"}
