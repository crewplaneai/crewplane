from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator_cli.core.config import Config, Settings
from orchestrator_cli.core.versions import CONFIG_SCHEMA_VERSION

from .runtime_config_redaction import (
    integration_with_sensitive_option_fingerprints,
    redact_sensitive_config,
    redact_sensitive_config_with_fingerprints,
    redacted_option_value,
    sensitive_integration_option_keys,
    sensitive_integration_option_paths,
)
from .signatures import signature_for_payload

SignatureScope = Literal["execution", "artifact", "observer", "validation"]


class CanonicalIntegrationConfig(BaseModel):
    """Adapter-selected options after side-effect-free canonicalization."""

    model_config = ConfigDict(extra="forbid")

    implementation: str
    resolved_identity: str
    api_version: str
    options: dict[str, Any] = Field(default_factory=dict)
    sensitive_options: list[str] = Field(default_factory=list)
    option_fingerprints: list[dict[str, str]] = Field(default_factory=list)
    option_scopes: dict[str, SignatureScope] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)

    def scoped_payload(self, scopes: set[SignatureScope]) -> dict[str, Any]:
        scoped_options = {
            key: value
            for key, value in self.options.items()
            if self.option_scopes.get(key) in scopes
        }
        return {
            "api_version": self.api_version,
            "capabilities": self.capabilities,
            "implementation": self.implementation,
            "options": scoped_options,
            "resolved_identity": self.resolved_identity,
        }

    def redacted_payload(self) -> dict[str, Any]:
        sensitive_keys = sensitive_integration_option_keys(self)
        return {
            "api_version": self.api_version,
            "capabilities": self.capabilities,
            "implementation": self.implementation,
            "option_fingerprints": self.option_fingerprints,
            "option_scopes": self.option_scopes,
            "options": {
                key: redacted_option_value(value) if key in sensitive_keys else value
                for key, value in self.options.items()
            },
            "resolved_identity": self.resolved_identity,
            "sensitive_options": sorted(sensitive_keys),
        }


class RuntimeConfigSnapshotOptions(BaseModel):
    """CLI/UI-derived flags classified before signature computation."""

    model_config = ConfigDict(extra="forbid")

    no_live: bool = False
    console_is_terminal: bool = False


class RuntimeConfigSnapshot(BaseModel):
    """Core-owned runtime config input to preflight compilation."""

    model_config = ConfigDict(extra="forbid")

    config_schema_version: str = CONFIG_SCHEMA_VERSION
    workflow_schema_version: str
    execution: dict[str, Any] = Field(default_factory=dict)
    agents: dict[str, Any] = Field(default_factory=dict)
    invoker: CanonicalIntegrationConfig
    artifacts: CanonicalIntegrationConfig
    ui: CanonicalIntegrationConfig
    validation: dict[str, Any] = Field(default_factory=dict)
    observer: dict[str, Any] = Field(default_factory=dict)
    sensitive_config_paths: list[str] = Field(default_factory=list)
    config_fingerprints: list[dict[str, str]] = Field(default_factory=list)
    effective_runtime_config_signature: str
    raw_agents: dict[str, Any] = Field(default_factory=dict, exclude=True)
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
        workflow_schema_version: str,
        invoker: CanonicalIntegrationConfig,
        artifacts: CanonicalIntegrationConfig,
        ui: CanonicalIntegrationConfig,
        options: RuntimeConfigSnapshotOptions,
    ) -> RuntimeConfigSnapshot:
        settings = config.settings if config.settings is not None else Settings()
        execution = {
            "log_level": settings.log_level,
            "max_audit_rounds": settings.max_audit_rounds,
            "max_concurrent_nodes": settings.max_concurrent_nodes,
            "max_parallel_invocations": settings.max_parallel_invocations,
            "sequential_consensus_on_exhaustion": settings.sequential_consensus_on_exhaustion,
            "token_budget": settings.token_budget.model_dump(
                mode="json", exclude_none=True
            ),
        }
        raw_agents = {
            name: agent.model_dump(mode="json", exclude_none=True)
            for name, agent in sorted(config.agents.items())
        }
        agents, sensitive_agent_paths = redact_sensitive_config(raw_agents)
        sensitive_paths = sorted(
            [
                *sensitive_agent_paths,
                *sensitive_integration_option_paths("invoker", invoker),
                *sensitive_integration_option_paths("artifacts", artifacts),
                *sensitive_integration_option_paths("ui", ui),
            ]
        )
        observer = {
            "console_is_terminal": options.console_is_terminal,
            "no_live": options.no_live,
        }
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
            "agents": agents,
            "artifacts": redacted_artifacts.scoped_payload({"artifact", "execution"}),
            "config_schema_version": config.version,
            "execution": execution,
            "invoker": redacted_invoker.scoped_payload({"execution", "artifact"}),
            "workflow_schema_version": workflow_schema_version,
        }
        return cls(
            config_schema_version=config.version,
            workflow_schema_version=workflow_schema_version,
            execution=execution,
            agents=agents,
            invoker=redacted_invoker,
            artifacts=redacted_artifacts,
            ui=redacted_ui,
            validation={"integrations_loaded": True},
            observer=observer,
            sensitive_config_paths=sensitive_paths,
            raw_agents=raw_agents,
            raw_invoker=invoker,
            raw_artifacts=artifacts,
            raw_ui=ui,
            effective_runtime_config_signature=signature_for_payload(payload),
        ).with_sensitive_config_fingerprints(None)

    def redacted_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
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
                "agents": agents,
                "invoker": invoker,
                "artifacts": artifacts,
                "ui": ui,
                "config_fingerprints": all_fingerprints,
                "effective_runtime_config_signature": self._effective_signature(
                    agents,
                    invoker,
                    artifacts,
                ),
            }
        )

    def _effective_signature(
        self,
        agents: dict[str, Any],
        invoker: CanonicalIntegrationConfig | None = None,
        artifacts: CanonicalIntegrationConfig | None = None,
    ) -> str:
        signature_invoker = invoker or self.invoker
        signature_artifacts = artifacts or self.artifacts
        payload = {
            "agents": agents,
            "artifacts": signature_artifacts.scoped_payload({"artifact", "execution"}),
            "config_schema_version": self.config_schema_version,
            "execution": self.execution,
            "invoker": signature_invoker.scoped_payload({"execution", "artifact"}),
            "workflow_schema_version": self.workflow_schema_version,
        }
        return signature_for_payload(payload)
