from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    JsonObject,
    JsonValue,
    PromptTransport,
    ProviderKind,
)
from crewplane.core.config import (
    DEFAULT_INVOCATION_IDLE_TIMEOUT_SECONDS,
    DEFAULT_INVOCATION_TIMEOUT_SECONDS,
    Config,
    FileAccessSettings,
    ProviderIdentifier,
    TokenPricing,
)
from crewplane.core.token_budget import TokenBudgetSettings
from crewplane.core.workflow.keywords import ProviderRole, SequentialConsensusPolicy
from crewplane.core.workspace.cache import workspace_cache_root
from crewplane.core.workspace.policy import WorkspaceCleanStart, WorktreeContract
from crewplane.core.workspace.settings import (
    DEFAULT_WORKSPACE_SETUP_TIMEOUT_SECONDS,
    WorkspaceDiskGuardrails,
    WorkspaceIdentitySettings,
    WorkspaceSettings,
)
from crewplane.version import SCHEMA_VERSION

from ..signatures import signature_for_payload
from .redaction import (
    integration_with_sensitive_option_fingerprints,
    redact_sensitive_config,
    redact_sensitive_config_with_fingerprints,
    sensitive_integration_option_paths,
)

if TYPE_CHECKING:
    from ..compiled_records import PreflightExecutionNode


class RuntimeConfigSnapshotOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    no_live: bool = False
    console_is_terminal: bool = False


class RuntimeExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_level: str
    max_audit_rounds: int
    max_expanded_nodes: int = Field(default=10_000, strict=True, gt=0)
    max_concurrent_nodes: int | None = None
    max_parallel_invocations: int | None = None
    sequential_consensus_on_exhaustion: SequentialConsensusPolicy
    token_budget: TokenBudgetSettings


class RuntimeFileAccessSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_template_paths: list[str] = Field(default_factory=list)


class RuntimeWorkspaceSettingsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    cache_root: str | None = None
    cleanup_on_success: bool = True
    worktree_contract: WorktreeContract = Field(default_factory=WorktreeContract)
    clean_start: WorkspaceCleanStart = "strict"
    setup_profiles: JsonObject = Field(default_factory=dict)
    setup_timeout_seconds: FiniteFloat = DEFAULT_WORKSPACE_SETUP_TIMEOUT_SECONDS
    identity: WorkspaceIdentitySettings = Field(
        default_factory=WorkspaceIdentitySettings
    )
    max_concurrent_materializations: int = 1
    disk: WorkspaceDiskGuardrails = Field(default_factory=WorkspaceDiskGuardrails)


class RuntimeAgentConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cli_cmd: list[str | JsonObject]
    environment: dict[str, str | JsonObject] = Field(default_factory=dict)
    provider_kind: ProviderIdentifier = ProviderKind.GENERIC.value
    default_model: str | None = None
    model_arg: str | JsonObject | None = "--model"
    prompt_transport: PromptTransport = "stdin"
    prompt_transport_arg: str | JsonObject | None = None
    extra_args: list[str | JsonObject] = Field(default_factory=list)
    max_retries: int = 0
    retry_delay_seconds: FiniteFloat = 300.0
    retry_on_exit_codes: list[int] = Field(default_factory=list)
    retry_on_stderr_contains: list[str] = Field(default_factory=list)
    retry_on_output_contains: list[str] = Field(default_factory=list)
    quota_reached_on_contains: list[str] = Field(default_factory=list)
    quota_reached_retry_delay_seconds: FiniteFloat = 300.0
    quota_reset_sleep_floor_seconds: FiniteFloat = 5.0
    quota_retry_max_wait_seconds: FiniteFloat | None = Field(default=None, gt=0)
    quota_retry_max_attempts: int | None = Field(default=None, ge=1)
    invocation_timeout_seconds: FiniteFloat | None = DEFAULT_INVOCATION_TIMEOUT_SECONDS
    invocation_idle_timeout_seconds: FiniteFloat | None = (
        DEFAULT_INVOCATION_IDLE_TIMEOUT_SECONDS
    )
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
    # Empty overrides preserve execution identities authored before this field.
    if payload.get("environment") == {}:
        payload.pop("environment")
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


def _failure_retry_can_schedule(config: RuntimeAgentConfigSnapshot) -> bool:
    return config.max_retries > 0 and (
        bool(config.retry_on_exit_codes)
        or bool(config.retry_on_stderr_contains)
        or bool(config.retry_on_output_contains)
    )


def execution_effective_payload(execution: RuntimeExecutionSnapshot) -> JsonObject:
    payload = cast(JsonObject, execution.model_dump(mode="json"))
    payload.pop("log_level", None)
    payload.pop("max_audit_rounds", None)
    return payload


def runtime_agent_effective_payload(config: RuntimeAgentConfigSnapshot) -> JsonObject:
    payload = runtime_agent_snapshot_payload(config)
    payload.pop("default_model", None)
    if not _failure_retry_can_schedule(config):
        payload.pop("max_retries", None)
        payload.pop("retry_delay_seconds", None)
    if config.provider_kind != ProviderKind.GENERIC:
        payload.pop("model_arg", None)
    return payload


def runtime_agent_invocation_effective_payload(
    config: RuntimeAgentConfigSnapshot,
    resolved_model: str | None,
) -> JsonObject:
    payload = runtime_agent_effective_payload(config)
    if resolved_model is None:
        payload.pop("model_arg", None)
    return payload


def runtime_agent_signature_payload(
    agent_config_key: str,
    agent_snapshot: RuntimeAgentConfigSnapshot | None,
    resolved_model: str | None,
) -> JsonObject:
    return {
        "agent_config": (
            runtime_agent_invocation_effective_payload(
                agent_snapshot,
                resolved_model,
            )
            if agent_snapshot is not None
            else None
        ),
        "agent_config_key": agent_config_key,
        "model": resolved_model,
    }


def runtime_agent_execution_payload(config: RuntimeAgentConfigSnapshot) -> JsonObject:
    return cast(JsonObject, config.model_dump(mode="json", exclude_none=False))


def runtime_agent_snapshot_payloads(
    agents: Mapping[str, RuntimeAgentConfigSnapshot],
) -> JsonObject:
    return {
        name: runtime_agent_snapshot_payload(agent)
        for name, agent in sorted(agents.items())
    }


def runtime_agent_effective_payloads(
    agents: Mapping[str, RuntimeAgentConfigSnapshot],
) -> JsonObject:
    return {
        name: runtime_agent_effective_payload(agent)
        for name, agent in sorted(agents.items())
    }


class RuntimeConfigSnapshot(BaseModel):
    """Core-owned runtime config input to preflight compilation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    execution: RuntimeExecutionSnapshot
    file_access: RuntimeFileAccessSnapshot = Field(
        default_factory=RuntimeFileAccessSnapshot
    )
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
    raw_agents: JsonObject = Field(default_factory=dict, exclude=True, repr=False)
    raw_workspace: JsonObject = Field(default_factory=dict, exclude=True, repr=False)
    raw_invoker: CanonicalIntegrationConfig | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    raw_artifacts: CanonicalIntegrationConfig | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    raw_ui: CanonicalIntegrationConfig | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )

    @classmethod
    def build(
        cls,
        config: Config,
        invoker: CanonicalIntegrationConfig,
        artifacts: CanonicalIntegrationConfig,
        ui: CanonicalIntegrationConfig,
        options: RuntimeConfigSnapshotOptions,
        file_access: FileAccessSettings | None = None,
    ) -> RuntimeConfigSnapshot:
        settings = config.settings
        resolved_file_access = (
            settings.file_access if file_access is None else file_access
        )
        execution = RuntimeExecutionSnapshot(
            log_level=settings.log_level,
            max_audit_rounds=settings.max_audit_rounds,
            max_expanded_nodes=settings.max_expanded_nodes,
            max_concurrent_nodes=settings.max_concurrent_nodes,
            max_parallel_invocations=settings.max_parallel_invocations,
            sequential_consensus_on_exhaustion=settings.sequential_consensus_on_exhaustion,
            token_budget=settings.token_budget,
        )
        raw_agents: JsonObject = {
            name: agent_config_input_payload(agent)
            for name, agent in sorted(config.agents.items())
        }
        _, sensitive_agent_paths = redact_sensitive_config(raw_agents)
        agents, _ = redact_sensitive_config_with_fingerprints(raw_agents, None)
        agent_snapshots = runtime_agent_snapshots(agents)
        raw_workspace: JsonObject = {
            "setup_profiles": settings.workspace.model_dump(mode="json")[
                "setup_profiles"
            ]
        }
        _, sensitive_workspace_paths = redact_sensitive_config(
            raw_workspace,
            ("workspace",),
        )
        redacted_workspace, _ = redact_sensitive_config_with_fingerprints(
            raw_workspace, None, ("workspace",)
        )
        integrations = (("invoker", invoker), ("artifacts", artifacts), ("ui", ui))
        sensitive_paths = sorted(
            sensitive_agent_paths
            + sensitive_workspace_paths
            + [
                path
                for name, value in integrations
                for path in sensitive_integration_option_paths(name, value)
            ]
        )
        setup_profiles = cast(JsonObject, redacted_workspace["setup_profiles"])
        workspace = runtime_workspace_snapshot(
            settings.workspace,
            setup_profiles,
        )
        if sensitive_paths:
            workspace = workspace.model_copy(update={"setup_profiles": setup_profiles})
        redacted_invoker, redacted_artifacts, redacted_ui = (
            integration_with_sensitive_option_fingerprints(value, name, None)[0]
            for name, value in integrations
        )
        file_access_snapshot = RuntimeFileAccessSnapshot.model_validate(
            resolved_file_access.model_dump(mode="python")
        )
        return cls(
            schema_version=config.version,
            execution=execution,
            file_access=file_access_snapshot,
            agents=agent_snapshots,
            invoker=redacted_invoker,
            artifacts=redacted_artifacts,
            ui=redacted_ui,
            validation=RuntimeValidationSnapshot(integrations_loaded=True),
            observer=RuntimeObserverSnapshot(
                console_is_terminal=options.console_is_terminal, no_live=options.no_live
            ),
            workspace=workspace,
            sensitive_config_paths=sensitive_paths,
            raw_agents=raw_agents,
            raw_workspace=raw_workspace,
            raw_invoker=invoker,
            raw_artifacts=artifacts,
            raw_ui=ui,
            effective_runtime_config_signature=_effective_signature(
                config.version,
                execution,
                file_access_snapshot,
                agent_snapshots,
                redacted_invoker,
                redacted_artifacts,
                workspace,
            ),
        )

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
                    "effective_runtime_config_signature": _effective_signature(
                        self.schema_version,
                        self.execution,
                        self.file_access,
                        self.agents,
                        self.invoker,
                        self.artifacts,
                        self.workspace,
                    )
                }
            )
        raw_agents = self.raw_agents or {
            name: runtime_agent_snapshot_payload(agent)
            for name, agent in self.agents.items()
        }
        agents, fingerprints = redact_sensitive_config_with_fingerprints(
            raw_agents,
            fingerprint_key,
        )
        agent_snapshots = runtime_agent_snapshots(agents)
        workspace_payload, workspace_fingerprints = (
            redact_sensitive_config_with_fingerprints(
                self.raw_workspace or {"setup_profiles": self.workspace.setup_profiles},
                fingerprint_key,
                ("workspace",),
            )
        )
        workspace = self.workspace.model_copy(
            update={"setup_profiles": workspace_payload["setup_profiles"]}
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
                *workspace_fingerprints,
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
                "workspace": workspace,
                "config_fingerprints": all_fingerprints,
                "effective_runtime_config_signature": _effective_signature(
                    self.schema_version,
                    self.execution,
                    self.file_access,
                    agent_snapshots,
                    invoker,
                    artifacts,
                    workspace,
                ),
            }
        )


def _effective_signature(
    schema_version: str,
    execution: RuntimeExecutionSnapshot,
    file_access: RuntimeFileAccessSnapshot,
    agents: Mapping[str, RuntimeAgentConfigSnapshot],
    invoker: CanonicalIntegrationConfig,
    artifacts: CanonicalIntegrationConfig,
    workspace: RuntimeWorkspaceSettingsSnapshot,
) -> str:
    return signature_for_payload(
        {
            "agents": runtime_agent_effective_payloads(agents),
            "artifacts": artifacts.scoped_payload({"artifact", "execution"}),
            "execution": execution_effective_payload(execution),
            "file_access": file_access.model_dump(mode="json"),
            "invoker": invoker.scoped_payload({"execution", "artifact"}),
            "schema_version": schema_version,
            "workspace": workspace_signature_payload(workspace),
        }
    )


def runtime_workspace_snapshot(
    workspace: WorkspaceSettings,
    setup_profiles: JsonObject | None = None,
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
        setup_profiles=setup_profiles
        if setup_profiles is not None
        else cast(
            JsonObject,
            workspace.model_dump(mode="json")["setup_profiles"],
        ),
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
    nodes: list[PreflightExecutionNode],
) -> str:
    payload: JsonObject = {
        "agent_invocations": runtime_agent_invocation_payloads(
            snapshot.agents,
            nodes,
        ),
        "artifacts": snapshot.artifacts.scoped_payload({"artifact", "execution"}),
        "execution": execution_signature_payload(snapshot.execution, nodes),
        "file_access": snapshot.file_access.model_dump(mode="json"),
        "invoker": snapshot.invoker.scoped_payload({"execution", "artifact"}),
        "schema_version": snapshot.schema_version,
        "workspace": workspace_payload,
    }
    return signature_for_payload(payload)


def runtime_agent_invocation_payloads(
    agents: Mapping[str, RuntimeAgentConfigSnapshot],
    nodes: list[PreflightExecutionNode],
) -> list[JsonValue]:
    return [
        {
            "agent_config": runtime_agent_signature_payload(
                provider.agent_config_key,
                agents.get(provider.agent_config_key),
                provider.model,
            ),
            "node_id": node.id,
            "requested_reasoning": provider.requested_reasoning,
            "task_id": provider.task_id,
        }
        for node in nodes
        for provider in node.provider_records
    ]


def execution_signature_payload(
    execution: RuntimeExecutionSnapshot,
    nodes: list[PreflightExecutionNode],
) -> JsonObject:
    payload = execution_effective_payload(execution)
    has_review_loop = any(
        node.mode == "sequential"
        and any(
            provider.role == ProviderRole.REVIEWER for provider in node.provider_records
        )
        for node in nodes
    )
    if not has_review_loop:
        payload.pop("sequential_consensus_on_exhaustion", None)
    return payload
