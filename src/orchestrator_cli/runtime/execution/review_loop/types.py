from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from orchestrator_cli.architecture.contracts import AgentInvoker
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    ProviderRecord,
)
from orchestrator_cli.observability.events import (
    EventSink,
    ExecutionEvent,
    format_execution_event_log_line,
)

from ..common import CompiledRuntimeContext, ExecutionTelemetry
from ..consensus import EvaluatedReviewResult
from ..provider_display import ProviderCallDisplay

DEFAULT_REMEDIATION_DEPTH = 1
DEFAULT_AUDIT_ROUNDS = 1
REVIEW_LOOP_STATUS_FILE = "review-loop-status.json"
INVALID_CANDIDATE_EMPTY = "invalid_candidate.empty"
INVALID_CANDIDATE_REDIRECTED = "invalid_candidate.redirected"


class ReviewLoopStatusOutputEntry(TypedDict):
    task_id: str
    provider: str
    role: str
    path: str


class ReviewLoopStatusPayload(TypedDict):
    node_id: str
    executed_audit_rounds: int
    final_local_round_num: int
    consensus_reached: bool
    continued_after_consensus_exhaustion: bool
    invalid_candidate_round_count: int
    no_progress_round_count: int
    artifact_drift_warning_count: int
    canonical_executor_outputs: list[ReviewLoopStatusOutputEntry]
    reviewer_outputs: list[ReviewLoopStatusOutputEntry]


@dataclass(frozen=True)
class DriftCheckResult:
    warning_paths: tuple[Path, ...] = ()
    fatal_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ActivityWindow:
    is_exclusive: bool
    version: int | None


@dataclass
class DriftMonitoringWindow:
    node_snapshot: dict[Path, tuple[int, str]]
    shared_reserved_snapshot: dict[Path, tuple[int, str]] | None
    summary_before: bytes | None
    event_log_before: bytes | None
    activity_window: ActivityWindow


@dataclass
class EventLogAppendCapture:
    event_sink: EventSink | None
    events: list[ExecutionEvent]

    def emit(self, event: ExecutionEvent) -> None:
        self.events.append(event)
        if self.event_sink is not None:
            self.event_sink(event)

    def expected_append_bytes_since(self, start_index: int) -> bytes:
        if self.event_sink is None:
            return b""
        return "".join(
            format_execution_event_log_line(event)
            for event in self.events[start_index:]
        ).encode("utf-8")

    def event_count(self) -> int:
        return len(self.events)


@dataclass(frozen=True)
class DriftGuardSession:
    telemetry: ExecutionTelemetry | None
    event_log_capture: EventLogAppendCapture | None


@dataclass
class DriftGuardCallRequest:
    runtime_context: CompiledRuntimeContext
    output: ArtifactStorePort
    node: PreflightExecutionNode
    node_dir: Path
    invoker: AgentInvoker
    telemetry: ExecutionTelemetry | None
    audit_round_num: int | None
    round_num: int
    provider: ProviderRecord
    task_id: str
    prompt: str
    output_file: Path
    role_label: str
    findings_enabled: bool
    allowed_paths: set[Path]
    display: ProviderCallDisplay
    drift_session: DriftGuardSession | None = None


@dataclass(frozen=True)
class ExecutorRoundArtifact:
    provider: ProviderRecord
    task_id: str
    content: str
    output_file: Path


@dataclass(frozen=True)
class ReviewerRoundArtifact:
    provider: ProviderRecord
    task_id: str
    evaluation: EvaluatedReviewResult
    output_file: Path


@dataclass
class ExecutorRoundRunResult:
    outputs: list[ExecutorRoundArtifact]
    drift_warning_count: int


@dataclass
class ReviewerRoundRunResult:
    outputs: list[ReviewerRoundArtifact]
    drift_warning_count: int
    reviewer_failure_count: int = 0


@dataclass(frozen=True)
class ReviewerInvocationResult:
    index: int
    provider: ProviderRecord
    task_id: str
    output_file: Path
    drift_warning_count: int


@dataclass(frozen=True)
class ReviewerInvocationFailure:
    index: int
    provider: ProviderRecord
    task_id: str
    output_file: Path
    error: Exception
    failure_kind: str
    warning: str


@dataclass(frozen=True)
class CandidateValidationResult:
    valid: bool
    reason: str | None = None
    invalid_task_ids: tuple[str, ...] = ()


@dataclass
class AuditRoundResult:
    consensus_reached: bool
    clean_fresh_approval: bool
    latest_executor_outputs: list[ExecutorRoundArtifact] | None
    latest_reviewer_outputs: list[ReviewerRoundArtifact]
    invalid_candidate_round_count: int
    no_progress_round_count: int
    artifact_drift_warning_count: int
    last_round_num: int


@dataclass
class AuditRoundProgress:
    executor_outputs: list[ExecutorRoundArtifact]
    previous_executor_outputs: list[ExecutorRoundArtifact] | None = None
    previous_review_packet: str | None = None
    previous_unresolved_fingerprints: tuple[str, ...] = ()
    previous_executor_fingerprint: str | None = None
    latest_valid_executor_outputs: list[ExecutorRoundArtifact] | None = None
    latest_reviewer_outputs: list[ReviewerRoundArtifact] = field(default_factory=list)
    invalid_candidate_round_count: int = 0
    no_progress_round_count: int = 0
    artifact_drift_warning_count: int = 0
    last_round_num: int = 0

    def add_artifact_drift_warnings(self, count: int) -> None:
        self.artifact_drift_warning_count += count

    def record_invalid_candidate(self) -> None:
        self.invalid_candidate_round_count += 1

    def record_no_progress(self) -> None:
        self.no_progress_round_count += 1

    def record_review_outputs(
        self,
        reviewer_outputs: list[ReviewerRoundArtifact],
    ) -> None:
        self.latest_reviewer_outputs = reviewer_outputs

    def advance_review_state(
        self,
        current_review_packet: str | None,
        current_unresolved_fingerprints: tuple[str, ...],
        current_executor_fingerprint: str,
    ) -> None:
        self.previous_executor_outputs = self.executor_outputs
        self.previous_review_packet = current_review_packet
        self.previous_unresolved_fingerprints = current_unresolved_fingerprints
        self.previous_executor_fingerprint = current_executor_fingerprint

    def to_result(
        self,
        consensus_reached: bool,
        clean_fresh_approval: bool,
    ) -> AuditRoundResult:
        return AuditRoundResult(
            consensus_reached=consensus_reached,
            clean_fresh_approval=clean_fresh_approval,
            latest_executor_outputs=self.latest_valid_executor_outputs,
            latest_reviewer_outputs=self.latest_reviewer_outputs,
            invalid_candidate_round_count=self.invalid_candidate_round_count,
            no_progress_round_count=self.no_progress_round_count,
            artifact_drift_warning_count=self.artifact_drift_warning_count,
            last_round_num=self.last_round_num,
        )


@dataclass
class ReviewLoopProgress:
    latest_executor_outputs: list[ExecutorRoundArtifact] | None = None
    latest_reviewer_outputs: list[ReviewerRoundArtifact] = field(default_factory=list)
    executed_audit_rounds: int = 0
    last_round_num: int = 0
    consensus_reached: bool = False
    continued_after_exhaustion: bool = False
    invalid_candidate_round_count: int = 0
    no_progress_round_count: int = 0
    artifact_drift_warning_count: int = 0

    def record_initial_executor_run(self, executor_run: ExecutorRoundRunResult) -> None:
        self.artifact_drift_warning_count += executor_run.drift_warning_count

    def record_audit_result(self, audit_result: AuditRoundResult) -> None:
        self.invalid_candidate_round_count += audit_result.invalid_candidate_round_count
        self.no_progress_round_count += audit_result.no_progress_round_count
        self.artifact_drift_warning_count += audit_result.artifact_drift_warning_count
        self.last_round_num = audit_result.last_round_num
        self.consensus_reached = audit_result.consensus_reached
        self.continued_after_exhaustion = False

        if audit_result.latest_executor_outputs is not None:
            self.latest_executor_outputs = audit_result.latest_executor_outputs
        if audit_result.latest_reviewer_outputs:
            self.latest_reviewer_outputs = audit_result.latest_reviewer_outputs

    def mark_consensus_exhausted(self, continued: bool) -> None:
        self.consensus_reached = False
        self.continued_after_exhaustion = continued


@dataclass
class ExecutorRoundRequest:
    runtime_context: CompiledRuntimeContext
    node: PreflightExecutionNode
    output: ArtifactStorePort
    node_dir: Path
    invoker: AgentInvoker
    telemetry: ExecutionTelemetry | None
    executors: tuple[ProviderRecord, ...]
    artifact_dir: Path
    executor_prompt: str
    previous_review_packet: str | None
    previous_executor_outputs: list[ExecutorRoundArtifact] | None
    audit_round_num: int | None
    round_num: int


@dataclass
class ReviewerRoundRequest:
    runtime_context: CompiledRuntimeContext
    node: PreflightExecutionNode
    output: ArtifactStorePort
    node_dir: Path
    invoker: AgentInvoker
    telemetry: ExecutionTelemetry | None
    reviewers: tuple[ProviderRecord, ...]
    artifact_dir: Path
    reviewer_prompt_context: str
    review_context: str
    previous_review_packet: str | None
    audit_round_num: int | None
    round_num: int


@dataclass
class ReviewerRoundRuntime:
    reviewer_prompt: str
    invocation_semaphore: asyncio.Semaphore | None
    drift_session: DriftGuardSession
    allowed_paths: set[Path]


@dataclass
class AuditRoundRequest:
    runtime_context: CompiledRuntimeContext
    stage: PreflightExecutionNode
    output: ArtifactStorePort
    node_dir: Path
    invoker: AgentInvoker
    telemetry: ExecutionTelemetry | None
    executors: tuple[ProviderRecord, ...]
    reviewers: tuple[ProviderRecord, ...]
    executor_prompt: str
    reviewer_prompt_context: str
    audit_dir: Path
    remediation_depth: int
    initial_executor_outputs: list[ExecutorRoundArtifact]
    audit_round_num: int | None


@dataclass
class ReviewLoopRunContext:
    runtime_context: CompiledRuntimeContext
    stage: PreflightExecutionNode
    output: ArtifactStorePort
    node_dir: Path
    invoker: AgentInvoker
    telemetry: ExecutionTelemetry | None
    executors: tuple[ProviderRecord, ...]
    reviewers: tuple[ProviderRecord, ...]
    executor_prompt: str
    reviewer_prompt_context: str
    remediation_depth: int
    audit_rounds: int


@dataclass
class ReviewRoundState:
    reviewer_outputs: list[ReviewerRoundArtifact]
    reviewer_failure_count: int
    current_review_packet: str | None
    current_unresolved_fingerprints: tuple[str, ...]
    current_executor_fingerprint: str
