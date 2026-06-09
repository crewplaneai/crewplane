from __future__ import annotations

from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from orchestrator_cli.architecture.contracts import (
    CanonicalIntegrationConfig,
    JsonObject,
    PromptTransport,
    ProviderKind,
)
from orchestrator_cli.core.config import Config, Settings, TokenPricing
from orchestrator_cli.core.token_budget import TokenBudgetSettings
from orchestrator_cli.core.workflow_keywords import SequentialConsensusPolicy
from orchestrator_cli.version import SCHEMA_VERSION

from .runtime_config_redaction import (
    integration_with_sensitive_option_fingerprints,
    redact_sensitive_config,
    redact_sensitive_config_with_fingerprints,
    sensitive_integration_option_paths,
)
from .signatures import signature_for_payload


class RuntimeConfigSnapshotOptions(BaseModel):
    """CLI/UI-derived flags classified before signature computation."""

    model_config = ConfigDict(extra="forbid")

    no_live: bool = False
    console_is_terminal: bool = False


class RuntimeExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_level: str
    max_audit_rounds: int
    max_concurrent_nodes: int | None = None
    max_parallel_invocations: int | None = None
    sequential_consensus_on_exhaustion: SequentialConsensusPolicy
    token_budget: TokenBudgetSettings


class RuntimeAgentConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cli_cmd: list[str]
    provider_kind: ProviderKind = "generic"
    default_model: str | None = None
    model_arg: str | None = "--model"
    prompt_transport: PromptTransport = "stdin"
    prompt_transport_arg: str | None = None
    extra_args: list[str | JsonObject] = Field(default_factory=list)
    max_retries: int = 0
    retry_delay_seconds: float = 300.0
    retry_on_exit_codes: list[int] = Field(default_factory=list)
    retry_on_stderr_contains: list[str] = Field(default_factory=list)
    retry_on_output_contains: list[str] = Field(default_factory=list)
    quota_reached_on_contains: list[str] = Field(default_factory=list)
    quota_reached_retry_delay_seconds: float = 300.0
    quota_reset_sleep_floor_seconds: float = 5.0
    invocation_timeout_seconds: float | None = None
    invocation_idle_timeout_seconds: float | None = 1800.0
    pricing: TokenPricing = Field(default_factory=TokenPricing)


class RuntimeValidationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integrations_loaded: bool = True


class RuntimeObserverSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    console_is_terminal: bool = False
    no_live: bool = False


def runtime_agent_snapshots(
    agents: JsonObject,
) -> dict[str, RuntimeAgentConfigSnapshot]:
    return {
        name: RuntimeAgentConfigSnapshot.model_validate(payload)
        for name, payload in agents.items()
    }


class RuntimeConfigSnapshot(BaseModel):
    """Core-owned runtime config input to preflight compilation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    execution: RuntimeExecutionSnapshot
    agents: dict[str, RuntimeAgentConfigSnapshot] = Field(default_factory=dict)
    invoker: CanonicalIntegrationConfig
    artifacts: CanonicalIntegrationConfig
    ui: CanonicalIntegrationConfig
    validation: RuntimeValidationSnapshot = Field(
        default_factory=RuntimeValidationSnapshot
    )
    observer: RuntimeObserverSnapshot = Field(default_factory=RuntimeObserverSnapshot)
    sensitive_config_paths: list[str] = Field(default_factory=list)
    config_fingerprints: list[dict[str, str]] = Field(default_factory=list)
    effective_runtime_config_signature: str
    raw_agents: JsonObject = Field(default_factory=dict, exclude=True)
    raw_invoker: CanonicalIntegrationConfig | None = Field(
        default=None,
        exclude=True,
    )
    raw_artifacts: CanonicalIntegrationConfig | None = Field(
        default=None,
        exclude=True,
    )
    raw_ui: CanonicalIntegrationConfig | None = Field(default=None, exclude=True)

    @classmethod
    def build(
        cls,
        config: Config,
        invoker: CanonicalIntegrationConfig,
        artifacts: CanonicalIntegrationConfig,
        ui: CanonicalIntegrationConfig,
        options: RuntimeConfigSnapshotOptions,
    ) -> RuntimeConfigSnapshot:
        settings = config.settings if config.settings is not None else Settings()
        execution = RuntimeExecutionSnapshot(
            log_level=settings.log_level,
            max_audit_rounds=settings.max_audit_rounds,
            max_concurrent_nodes=settings.max_concurrent_nodes,
            max_parallel_invocations=settings.max_parallel_invocations,
            sequential_consensus_on_exhaustion=settings.sequential_consensus_on_exhaustion,
            token_budget=settings.token_budget,
        )
        raw_agents = {
            name: agent.model_dump(mode="json", exclude_none=True)
            for name, agent in sorted(config.agents.items())
        }
        agents, sensitive_agent_paths = redact_sensitive_config(raw_agents)
        agent_snapshots = runtime_agent_snapshots(agents)
        sensitive_paths = sorted(
            [
                *sensitive_agent_paths,
                *sensitive_integration_option_paths("invoker", invoker),
                *sensitive_integration_option_paths("artifacts", artifacts),
                *sensitive_integration_option_paths("ui", ui),
            ]
        )
        observer = RuntimeObserverSnapshot(
            console_is_terminal=options.console_is_terminal,
            no_live=options.no_live,
        )
        redacted_invoker, _ = integration_with_sensitive_option_fingerprints(
            invoker,
            "invoker",
            None,
        )
        redacted_artifacts, _ = integration_with_sensitive_option_fingerprints(
            artifacts,
            "artifacts",
            None,
        )
        redacted_ui, _ = integration_with_sensitive_option_fingerprints(
            ui,
            "ui",
            None,
        )
        payload = {
            "agents": agent_snapshots,
            "artifacts": redacted_artifacts.scoped_payload({"artifact", "execution"}),
            "execution": execution,
            "invoker": redacted_invoker.scoped_payload({"execution", "artifact"}),
            "schema_version": config.version,
        }
        return cls(
            schema_version=config.version,
            execution=execution,
            agents=agent_snapshots,
            invoker=redacted_invoker,
            artifacts=redacted_artifacts,
            ui=redacted_ui,
            validation=RuntimeValidationSnapshot(integrations_loaded=True),
            observer=observer,
            sensitive_config_paths=sensitive_paths,
            raw_agents=raw_agents,
            raw_invoker=invoker,
            raw_artifacts=artifacts,
            raw_ui=ui,
            effective_runtime_config_signature=signature_for_payload(payload),
        ).with_sensitive_config_fingerprints(None)

    def redacted_payload(self) -> JsonObject:
        payload = cast(JsonObject, self.model_dump(mode="json", exclude_none=True))
        payload["invoker"] = self.invoker.redacted_payload()
        payload["artifacts"] = self.artifacts.redacted_payload()
        payload["ui"] = self.ui.redacted_payload()
        return payload

    def with_sensitive_config_fingerprints(
        self, fingerprint_key: bytes | None
    ) -> RuntimeConfigSnapshot:
        if not self.sensitive_config_paths:
            return self.model_copy(
                update={
                    "effective_runtime_config_signature": self._effective_signature(
                        self.agents
                    )
                }
            )
        agents, fingerprints = redact_sensitive_config_with_fingerprints(
            self.raw_agents or self.agents,
            fingerprint_key,
        )
        agent_snapshots = runtime_agent_snapshots(agents)
        invoker, invoker_fingerprints = integration_with_sensitive_option_fingerprints(
            self.raw_invoker or self.invoker,
            "invoker",
            fingerprint_key,
        )
        artifacts, artifact_fingerprints = (
            integration_with_sensitive_option_fingerprints(
                self.raw_artifacts or self.artifacts,
                "artifacts",
                fingerprint_key,
            )
        )
        ui, ui_fingerprints = integration_with_sensitive_option_fingerprints(
            self.raw_ui or self.ui,
            "ui",
            fingerprint_key,
        )
        all_fingerprints = sorted(
            [
                *fingerprints,
                *invoker_fingerprints,
                *artifact_fingerprints,
                *ui_fingerprints,
            ],
            key=lambda item: item["path"],
        )
        return self.model_copy(
            update={
                "agents": agent_snapshots,
                "invoker": invoker,
                "artifacts": artifacts,
                "ui": ui,
                "config_fingerprints": all_fingerprints,
                "effective_runtime_config_signature": self._effective_signature(
                    agent_snapshots,
                    invoker,
                    artifacts,
                ),
            }
        )

    def _effective_signature(
        self,
        agents: object,
        invoker: CanonicalIntegrationConfig | None = None,
        artifacts: CanonicalIntegrationConfig | None = None,
    ) -> str:
        signature_invoker = invoker or self.invoker
        signature_artifacts = artifacts or self.artifacts
        payload = {
            "agents": agents,
            "artifacts": signature_artifacts.scoped_payload({"artifact", "execution"}),
            "execution": self.execution,
            "invoker": signature_invoker.scoped_payload({"execution", "artifact"}),
            "schema_version": self.schema_version,
        }
        return signature_for_payload(payload)
