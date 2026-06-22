from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    ProviderRecord,
)
from orchestrator_cli.core.preflight.runtime_config import (
    RuntimeAgentConfigSnapshot,
    runtime_agent_execution_payload,
)
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.core.preflight.signatures import signature_for_payload
from orchestrator_cli.core.value_checks import positive_strict_int
from orchestrator_cli.runtime.workspace.materialization import MaterializationLimiter
from orchestrator_cli.runtime.workspace.reuse import WorktreeReuseCache

from .deferred_cleanup import DeferredAsyncCleanupRegistry
from .generated_file_workspaces import GeneratedFileWorkspaceRegistry


@dataclass
class CompiledRuntimeContext:
    plan: PreflightExecutionPlan
    secret_context: SecretContext
    generated_file_workspaces: GeneratedFileWorkspaceRegistry = field(
        default_factory=GeneratedFileWorkspaceRegistry
    )
    worktree_reuse_cache: WorktreeReuseCache = field(default_factory=WorktreeReuseCache)
    deferred_workspace_cleanups: DeferredAsyncCleanupRegistry = field(
        default_factory=DeferredAsyncCleanupRegistry
    )
    workspace_materialization_limiter: MaterializationLimiter = field(init=False)

    def __post_init__(self) -> None:
        self.workspace_materialization_limiter = MaterializationLimiter.from_plan(
            self.plan
        )

    def validate_execution_contract(self) -> None:
        for node in self.plan.nodes:
            for provider in node.provider_records:
                self.agent_config_for_provider(provider)

    def agent_config_for_provider(self, provider: ProviderRecord) -> AgentConfig:
        self._validate_provider_record(provider)
        agent_payload = agent_config_payload_from_plan(
            self.plan,
            provider.agent_config_key,
        )
        resolved_payload = resolve_secret_config_values(
            agent_payload,
            self.secret_context,
        )
        if not isinstance(resolved_payload, dict):
            raise ValueError("Runtime agent config metadata must be a mapping.")
        runtime_agent = RuntimeAgentConfigSnapshot.model_validate(resolved_payload)
        return AgentConfig(**runtime_agent_execution_payload(runtime_agent))

    def _validate_provider_record(self, provider: ProviderRecord) -> None:
        expected_agent_signature = agent_config_signature_from_plan(
            self.plan,
            provider.agent_config_key,
        )
        if expected_agent_signature is None:
            raise ValueError(
                "Compiled provider record references missing signed agent config "
                f"'{provider.agent_config_key}' for provider '{provider.provider}'."
            )
        if expected_agent_signature != provider.agent_config_signature:
            raise ValueError(
                "Compiled provider record agent config signature does not match "
                f"the preflight runtime snapshot for '{provider.agent_config_key}'."
            )

        expected_invoker_signature = invoker_config_signature_from_plan(self.plan)
        if expected_invoker_signature is None:
            raise ValueError("Compiled plan is missing signed invoker config metadata.")
        if expected_invoker_signature != provider.invoker_config_signature:
            raise ValueError(
                "Compiled provider record invoker config signature does not match "
                "the preflight runtime snapshot."
            )

    def max_concurrent_nodes(self) -> int | None:
        value = self._execution_setting("max_concurrent_nodes")
        return positive_strict_int(value)

    def max_parallel_invocations(self) -> int | None:
        value = self._execution_setting("max_parallel_invocations")
        return positive_strict_int(value)

    def sequential_consensus_on_exhaustion(self) -> str:
        value = self._execution_setting("sequential_consensus_on_exhaustion")
        if isinstance(value, str):
            return value
        return "continue"

    def _execution_setting(self, key: str) -> object:
        snapshot = self.plan.runtime_config_snapshot
        execution = snapshot.get("execution")
        if not isinstance(execution, dict):
            return None
        return execution.get(key)


def agent_config_signature_from_plan(
    plan: PreflightExecutionPlan,
    agent_config_key: str,
) -> str | None:
    agents = plan.runtime_config_snapshot.get("agents")
    if not isinstance(agents, dict) or agent_config_key not in agents:
        return None
    return signature_for_payload(
        {
            "agent_config": agents.get(agent_config_key),
            "agent_config_key": agent_config_key,
        }
    )


def agent_config_payload_from_plan(
    plan: PreflightExecutionPlan,
    agent_config_key: str,
) -> dict[str, object]:
    agents = plan.runtime_config_snapshot.get("agents")
    if not isinstance(agents, dict):
        raise ValueError("Compiled plan is missing runtime agent config metadata.")
    payload = agents.get(agent_config_key)
    if not isinstance(payload, dict):
        raise ValueError(
            "Compiled provider record references missing agent config "
            f"'{agent_config_key}'."
        )
    return payload


def resolve_secret_config_values(
    payload: object,
    secret_context: SecretContext,
) -> object:
    if isinstance(payload, list):
        return [resolve_secret_config_values(item, secret_context) for item in payload]
    if not isinstance(payload, dict):
        return payload
    if payload.get("redacted") is True:
        handle = payload.get("value_handle")
        if not isinstance(handle, str):
            raise ValueError(
                "Redacted runtime config value is missing a secret handle."
            )
        try:
            return secret_context.get(handle)
        except KeyError as exc:
            raise ValueError(
                f"Runtime secret handle '{handle}' is unavailable."
            ) from exc
    return {
        key: resolve_secret_config_values(value, secret_context)
        for key, value in payload.items()
    }


def invoker_config_signature_from_plan(plan: PreflightExecutionPlan) -> str | None:
    invoker = plan.runtime_config_snapshot.get("invoker")
    if not isinstance(invoker, dict):
        return None
    options = invoker.get("options")
    option_scopes = invoker.get("option_scopes")
    if not isinstance(options, dict) or not isinstance(option_scopes, dict):
        return None
    scoped_options = {
        key: value
        for key, value in options.items()
        if option_scopes.get(key) in {"execution", "artifact"}
    }
    return signature_for_payload(
        {
            "capabilities": invoker.get("capabilities", {}),
            "implementation": invoker.get("implementation"),
            "options": scoped_options,
            "resolved_identity": invoker.get("resolved_identity"),
        }
    )
