from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    JsonObject,
    PromptTransport,
    ProviderKind,
)
from crewplane.core.config import (
    Config,
    Settings,
    TokenPricing,
)
from crewplane.core.token_budget import TokenBudgetSettings
from crewplane.core.workflow.keywords import SequentialConsensusPolicy
from crewplane.core.workspace.cache import workspace_cache_root
from crewplane.core.workspace.policy import WorktreeContract
from crewplane.core.workspace.settings import (
    WorkspaceDiskGuardrails,
    WorkspaceIdentitySettings,
    WorkspaceSettings,
    WorkspaceSetupProfile,
)
from crewplane.version import SCHEMA_VERSION

from ..signatures import signature_for_payload
from .redaction import (
    integration_with_sensitive_option_fingerprints,
    redact_sensitive_config,
    redact_sensitive_config_with_fingerprints,
    sensitive_integration_option_paths,
)


class RuntimeConfigSnapshotOptions(BaseModel):
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


class RuntimeWorkspaceSettingsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    cache_root: str | None = None
    cleanup_on_success: bool = True
    worktree_contract: WorktreeContract = Field(default_factory=WorktreeContract)
    clean_start: str = "strict"
    setup_profiles: dict[str, WorkspaceSetupProfile] = Field(default_factory=dict)
    setup_timeout_seconds: float = 600.0
    identity: WorkspaceIdentitySettings = Field(
        default_factory=WorkspaceIdentitySettings
    )
    max_concurrent_materializations: int = 1
    disk: WorkspaceDiskGuardrails = Field(default_factory=WorkspaceDiskGuardrails)


class RuntimeAgentConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cli_cmd: list[str]
    provider_kind: ProviderKind = ProviderKind.GENERIC
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


def _field_default_is_none(config: BaseModel, field_name: str) -> bool:
    field = type(config).model_fields[field_name]
    if field.is_required():
        return False
    return field.get_default(call_default_factory=True) is None


def _model_payload_preserving_non_default_nulls(config: BaseModel) -> JsonObject:
    payload = cast(JsonObject, config.model_dump(mode="json", exclude_none=True))
    for field_name in sorted(config.model_fields_set):
        if getattr(config, field_name) is None and not _field_default_is_none(
            config,
            field_name,
        ):
            payload[field_name] = None
    return payload


def agent_config_input_payload(config: BaseModel) -> JsonObject:
    return _model_payload_preserving_non_default_nulls(config)


def runtime_agent_snapshot_payload(config: RuntimeAgentConfigSnapshot) -> JsonObject:
    return _model_payload_preserving_non_default_nulls(config)


def runtime_agent_execution_payload(config: RuntimeAgentConfigSnapshot) -> JsonObject:
    return cast(JsonObject, config.model_dump(mode="json", exclude_none=False))


def runtime_agent_snapshot_payloads(
    agents: Mapping[str, RuntimeAgentConfigSnapshot],
) -> dict[str, JsonObject]:
    return {
        name: runtime_agent_snapshot_payload(agent)
        for name, agent in sorted(agents.items())
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
    workspace: RuntimeWorkspaceSettingsSnapshot = Field(
        default_factory=RuntimeWorkspaceSettingsSnapshot
    )
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
            name: agent_config_input_payload(agent)
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
        workspace = runtime_workspace_snapshot(settings.workspace)
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
            "agents": runtime_agent_snapshot_payloads(agent_snapshots),
            "artifacts": redacted_artifacts.scoped_payload({"artifact", "execution"}),
            "execution": execution,
            "invoker": redacted_invoker.scoped_payload({"execution", "artifact"}),
            "schema_version": config.version,
            "workspace": workspace_signature_payload(workspace),
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
            workspace=workspace,
            sensitive_config_paths=sensitive_paths,
            raw_agents=raw_agents,
            raw_invoker=invoker,
            raw_artifacts=artifacts,
            raw_ui=ui,
            effective_runtime_config_signature=signature_for_payload(payload),
        ).with_sensitive_config_fingerprints(None)

    def redacted_payload(self) -> JsonObject:
        payload = cast(JsonObject, self.model_dump(mode="json", exclude_none=True))
        payload["agents"] = runtime_agent_snapshot_payloads(self.agents)
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
        agents: Mapping[str, RuntimeAgentConfigSnapshot],
        invoker: CanonicalIntegrationConfig | None = None,
        artifacts: CanonicalIntegrationConfig | None = None,
    ) -> str:
        signature_invoker = invoker or self.invoker
        signature_artifacts = artifacts or self.artifacts
        payload = {
            "agents": runtime_agent_snapshot_payloads(agents),
            "artifacts": signature_artifacts.scoped_payload({"artifact", "execution"}),
            "execution": self.execution,
            "invoker": signature_invoker.scoped_payload({"execution", "artifact"}),
            "schema_version": self.schema_version,
            "workspace": workspace_signature_payload(self.workspace),
        }
        return signature_for_payload(payload)


def runtime_workspace_snapshot(
    workspace: WorkspaceSettings,
) -> RuntimeWorkspaceSettingsSnapshot:
    if not workspace.enabled:
        return RuntimeWorkspaceSettingsSnapshot()
    return RuntimeWorkspaceSettingsSnapshot(
        enabled=workspace.enabled,
        cache_root=workspace.cache_root,
        cleanup_on_success=workspace.cleanup_on_success,
        worktree_contract=WorktreeContract(
            mode=workspace.worktree_contract,
            schema_version=SCHEMA_VERSION,
        ),
        clean_start=workspace.clean_start,
        setup_profiles=dict(sorted(workspace.setup_profiles.items())),
        setup_timeout_seconds=workspace.setup_timeout_seconds,
        identity=workspace.identity,
        max_concurrent_materializations=workspace.max_concurrent_materializations,
        disk=workspace.disk,
    )


def workspace_signature_payload(
    workspace: RuntimeWorkspaceSettingsSnapshot,
) -> JsonObject:
    if not workspace.enabled:
        return {"enabled": False}
    payload: JsonObject = {
        "clean_start": workspace.clean_start,
        "enabled": True,
        "worktree_contract": workspace.worktree_contract.model_dump(mode="json"),
    }
    if workspace.identity.include_cache_root:
        payload["cache_root"] = workspace_cache_root(workspace.cache_root).as_posix()
        payload["identity"] = workspace.identity.model_dump(mode="json")
    return payload


def runtime_config_signature(
    snapshot: RuntimeConfigSnapshot,
    workspace_payload: JsonObject,
) -> str:
    payload = {
        "agents": runtime_agent_snapshot_payloads(snapshot.agents),
        "artifacts": snapshot.artifacts.scoped_payload({"artifact", "execution"}),
        "execution": snapshot.execution,
        "invoker": snapshot.invoker.scoped_payload({"execution", "artifact"}),
        "schema_version": snapshot.schema_version,
        "workspace": workspace_payload,
    }
    return signature_for_payload(payload)
