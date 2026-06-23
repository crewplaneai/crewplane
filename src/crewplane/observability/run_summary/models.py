from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from crewplane.architecture.contracts import (
    AggregateCostConfidence,
    InvocationCostConfidence,
    OutputExtractionStatus,
    ProviderUsageStatus,
)


@dataclass(frozen=True)
class NodeCounts:
    pending: int
    running: int
    succeeded: int
    blocked: int
    failed: int


@dataclass(frozen=True)
class SpendTotals:
    terminal_invocations: int
    total_attempts: int
    cli_captured_invocations: int
    provider_usage_full_invocations: int
    provider_usage_partial_invocations: int
    provider_usage_malformed_invocations: int
    visible_estimate_tokens: int
    configured_cost_usd: float | None
    configured_cost_confidence: AggregateCostConfidence


@dataclass(frozen=True)
class SpendOverviewRow:
    label: str
    value: str
    confidence: AggregateCostConfidence | None = None
    show_in_terminal: bool = True

    def markdown_value(self) -> str:
        if self.confidence is None:
            return self.value
        return f"{self.value} (confidence: {self.confidence})"

    def terminal_value(self) -> str:
        if self.confidence is None:
            return self.value
        return f"{self.value} ({self.confidence})"


@dataclass(frozen=True)
class ProviderUsageRollup:
    provider: str
    terminal_invocations: int
    total_attempts: int
    cli_captured_invocations: int
    provider_usage_full_invocations: int
    provider_usage_partial_invocations: int
    provider_usage_malformed_invocations: int
    visible_estimate_tokens: int
    configured_cost_usd: float | None
    configured_cost_confidence: AggregateCostConfidence


@dataclass(frozen=True)
class UsageRollupValues:
    terminal_invocations: int
    total_attempts: int
    cli_captured_invocations: int
    provider_usage_full_invocations: int
    provider_usage_partial_invocations: int
    provider_usage_malformed_invocations: int
    visible_estimate_tokens: int
    configured_cost_usd: float | None
    configured_cost_confidence: AggregateCostConfidence


@dataclass(frozen=True)
class InvocationUsageSummary:
    provider: str
    node_id: str | None
    task_id: str | None
    audit_round_num: int | None
    round_num: int | None
    attempt_count: int
    cli_captured: bool
    output_extraction_status: OutputExtractionStatus
    provider_usage_status: ProviderUsageStatus
    provider_tokens: Mapping[str, int | None]
    visible_estimate_tokens: int | None
    visible_estimate_method: str | None
    visible_estimate_is_lower_bound: bool
    configured_cost_usd: float | None
    invocation_cost_confidence: InvocationCostConfidence
    usage_parse_error: str | None
    failure_kind: str | None
    failure_phase: str | None
    failure_source: str | None
    failure_advice: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_tokens",
            MappingProxyType(dict(self.provider_tokens)),
        )


@dataclass(frozen=True)
class NodeOutcomeSummary:
    node_id: str
    status: str
    duration_label: str
    result_path: Path | None


@dataclass(frozen=True)
class IssueSummary:
    level: str
    timestamp_utc: str
    message: str


@dataclass(frozen=True)
class ArtifactReferenceSummary:
    node_id: str
    task_id: str
    audit_round_num: int | None
    round_num: int | None
    output_file: str | None
    log_file: str | None


@dataclass(frozen=True)
class WorkspacePlanSummary:
    worktree_contract_mode: str | None
    worktree_contract_schema_version: str | None
    source_commit: str | None
    source_tree: str | None
    object_format: str | None
    clean_start: str | None
    invoker_implementation: str | None
    invoker_launch_mode: str | None
    invoker_controlled_child_environment: bool | None
    rendered_locator_count: int | None
    rendered_project_initial_count: int | None
    rendered_runtime_dynamic_count: int | None
    cleanup_on_success: bool | None
    cache_root_configured: bool | None
    planned_workspace_node_count: int


@dataclass(frozen=True)
class WorkspaceInvocationSourceSummary:
    kind: str | None = None
    node_id: str | None = None
    commit: str | None = None
    tree: str | None = None
    worktree_contract_mode: str | None = None
    worktree_contract_schema_version: str | None = None


@dataclass(frozen=True)
class WorkspaceInvocationExecutionSummary:
    cache_root: str | None = None
    workspace_path: str | None = None
    checkout_root: str | None = None
    effective_cwd: str | None = None
    checkout_size_bytes: int | None = None
    provisioning_duration_seconds: float | None = None


@dataclass(frozen=True)
class WorkspaceInvocationSetupSummary:
    profile_name: str | None = None
    status: str | None = None
    duration_seconds: float | None = None
    failure_message: str | None = None
    command_count: int | None = None
    log_path: str | None = None
    metadata_path: str | None = None


@dataclass(frozen=True)
class WorkspaceInvocationReuseSummary:
    strategy: str | None = None
    reused: bool | None = None
    fallback: bool | None = None
    fallback_reason: str | None = None
    previous_workspace_state: str | None = None
    reset_verification: str | None = None


@dataclass(frozen=True)
class WorkspaceInvocationBranchExportSummary:
    status: str | None = None
    operation: str | None = None
    branch_name: str | None = None
    branch_ref: str | None = None
    record_artifact: str | None = None
    failure_message: str | None = None


@dataclass(frozen=True)
class WorkspaceInvocationSummary:
    node_id: str | None
    task_id: str | None
    audit_round_num: int | None
    round_num: int | None
    workspace_kind: str | None
    logical_worktree_name: str | None
    status: str | None
    state_path: str | None
    writable: bool | None
    lineage_producer: bool | None
    child_environment_required: bool | None
    child_environment_applied: bool | None
    source: WorkspaceInvocationSourceSummary = field(
        default_factory=WorkspaceInvocationSourceSummary
    )
    execution: WorkspaceInvocationExecutionSummary = field(
        default_factory=WorkspaceInvocationExecutionSummary
    )
    setup: WorkspaceInvocationSetupSummary = field(
        default_factory=WorkspaceInvocationSetupSummary
    )
    reuse: WorkspaceInvocationReuseSummary = field(
        default_factory=WorkspaceInvocationReuseSummary
    )
    branch_export: WorkspaceInvocationBranchExportSummary = field(
        default_factory=WorkspaceInvocationBranchExportSummary
    )
    materialization: str | None = None
    retention: str | None = None
    result_commit: str | None = None
    result_tree: str | None = None
    candidate_commit: str | None = None
    candidate_tree: str | None = None
    final_head: str | None = None
    changed_path_count: int | None = None
    bundle_path: str | None = None
    bundle_size_bytes: int | None = None
    bundle_verified: bool | None = None
    rendered_file_count: int | None = None
    diagnostic_count: int | None = None
    snapshot_drift_discarded: bool | None = None
    snapshot_changed_paths_reported: int | None = None
    snapshot_changed_paths_truncated: bool | None = None
    checkpoint_count: int | None = None


@dataclass(frozen=True)
class WorkspaceRunSummary:
    plan: WorkspacePlanSummary | None
    invocations: tuple[WorkspaceInvocationSummary, ...]


@dataclass(frozen=True)
class RunSummary:
    workflow_name: str
    run_id: str
    workflow_status: str
    review_consensus_unresolved: bool
    started_at: str
    completed_at: str
    elapsed_label: str | None
    node_counts: NodeCounts
    spend: SpendTotals | None
    provider_rollups: tuple[ProviderUsageRollup, ...]
    invocation_usages: tuple[InvocationUsageSummary, ...]
    omitted_invocation_usage_count: int
    node_outcomes: tuple[NodeOutcomeSummary, ...]
    workspace: WorkspaceRunSummary | None
    issues: tuple[IssueSummary, ...]
    artifact_references: tuple[ArtifactReferenceSummary, ...]
    event_log_path: Path
    summary_path: Path


@dataclass(frozen=True)
class RunSummaryFacts:
    invocation_usages: tuple[InvocationUsageSummary, ...]
    workspace_invocations: tuple[WorkspaceInvocationSummary, ...]
    spend: SpendTotals | None
    provider_rollups: tuple[ProviderUsageRollup, ...]
    omitted_invocation_usage_count: int
    review_consensus_unresolved: bool
    started_at: str
    completed_at: str


class PersistentLoggerLifecycle(StrEnum):
    NEW = "new"
    RUNNING = "running"
    STOPPED = "stopped"
