from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from orchestrator_cli.runtime.agent.usage import (
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
    node_outcomes: tuple[NodeOutcomeSummary, ...]
    issues: tuple[IssueSummary, ...]
    artifact_references: tuple[ArtifactReferenceSummary, ...]
    event_log_path: Path
    summary_path: Path


class PersistentLoggerLifecycle(StrEnum):
    NEW = "new"
    RUNNING = "running"
    STOPPED = "stopped"
