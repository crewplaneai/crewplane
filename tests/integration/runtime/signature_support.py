from crewplane.core.preflight.runtime_config import (
    RuntimeAgentConfigSnapshot,
    runtime_agent_signature_payload,
)
from crewplane.core.preflight.signatures import signature_for_payload


def build_agent_signature(
    agent_config_key: str,
    agent_payload: object,
    resolved_model: str | None,
) -> str:
    agent_snapshot = RuntimeAgentConfigSnapshot.model_validate(agent_payload)
    return signature_for_payload(
        runtime_agent_signature_payload(
            agent_config_key,
            agent_snapshot,
            resolved_model,
        )
    )
