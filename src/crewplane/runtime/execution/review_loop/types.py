from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import TypedDict

from crewplane.architecture.contracts import AgentInvoker
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    ProviderRecord,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    EventSink,
    ExecutionEvent,
    format_execution_event_log_line,
)
from crewplane.runtime.execution.workspace_files import ResolvedWorkspaceFile

from ..common import CompiledRuntimeContext, ExecutionTelemetry
from ..consensus import EvaluatedReviewResult
from ..provider_call.display import ProviderCallDisplay
from ..provider_call.types import ProviderOutputPolicy
from ..publication_registry import RuntimePublicationRegistry

DEFAULT_REMEDIATION_DEPTH = 1
DEFAULT_AUDIT_ROUNDS = 1
REVIEW_LOOP_STATUS_FILE = "review-loop-status.json"
INVALID_CANDIDATE_EMPTY = "invalid_candidate.empty"
INVALID_CANDIDATE_REDIRECTED = "invalid_candidate.redirected"


class ReviewLoopStatusOutputEntry(TypedDict):
    task_id: str
    provider: str
    role: ProviderRole
    path: str
    sha256: str
    size_bytes: int
    audit_round_num: int | None
    round_num: int


class ReviewLoopStatusPayload(TypedDict):
    node_id: str
    executed_audit_rounds: int
    attempted_local_round_num: int
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
class DirectorySnapshot:
    mode: int
    device: int
    inode: int
    user_id: int
    group_id: int
    link_count: int
    changed_at_ns: int
    entry_names: tuple[str, ...]


@dataclass(frozen=True)
class ActivityWindow:
    is_exclusive: bool
    version: int | None


@dataclass(frozen=True)
class DriftRecoveryBaseline:
    node_snapshot: dict[Path, tuple[int, str]]
    shared_reserved_snapshot: dict[Path, tuple[int, str]]
    node_original_bytes: dict[Path, bytes]
    shared_reserved_original_bytes: dict[Path, bytes]
    node_directory_snapshot: dict[Path, DirectorySnapshot]
    shared_reserved_directory_snapshot: dict[Path, DirectorySnapshot]


@dataclass
class DriftMonitoringWindow:
    node_snapshot: dict[Path, tuple[int, str]]
    shared_reserved_snapshot: dict[Path, tuple[int, str]] | None
    summary_before: bytes | None
    event_log_before: bytes | None
    activity_window: ActivityWindow
    node_original_bytes: dict[Path, bytes] = field(default_factory=dict)
    shared_reserved_original_bytes: dict[Path, bytes] = field(default_factory=dict)
    node_directory_snapshot: dict[Path, DirectorySnapshot] = field(default_factory=dict)
    shared_reserved_directory_snapshot: dict[Path, DirectorySnapshot] | None = None
    node_original_directories: set[Path] = field(default_factory=set)
    shared_reserved_original_directories: set[Path] = field(default_factory=set)
    event_publication_cursor: int | None = None


@dataclass
class EventLogAppendCapture:
    event_sink: EventSink | None
    events: list[ExecutionEvent]
    owner_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    runtime_publications: RuntimePublicationRegistry | None = None

    def emit(self, event: ExecutionEvent) -> None:
        if self.event_sink is None:
            return
        line = format_execution_event_log_line(event).encode("utf-8")
        if self.runtime_publications is None:
            self.event_sink(event)
        else:
            with self.runtime_publications.event_publication(self.owner_id, line):
                self.event_sink(event)
        self.events.append(event)

    def expected_append_bytes_since(self, start_index: int) -> bytes:
        if self.event_sink is None:
            return b""
        return "".join(
            format_execution_event_log_line(event)
            for event in self.events[start_index:]
        ).encode("utf-8")

    def event_count(self) -> int:
        return len(self.events)


@dataclass
class GeneratedFileDriftAllowance:
    _in_progress_roots: set[Path] = field(default_factory=set, repr=False)
    _published_signatures: dict[Path, tuple[int, str]] = field(
        default_factory=dict,
        repr=False,
    )
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)
    _version: int = field(default=0, repr=False, compare=False)

    def start_snapshot(self, root: Path) -> None:
        with self._lock:
            self._in_progress_roots.add(root)
            self._version += 1

    def finish_snapshot(
        self,
        root: Path,
        published_signatures: dict[Path, tuple[int, str]] | None,
    ) -> None:
        with self._lock:
            if published_signatures is not None:
                self._published_signatures.update(published_signatures)
            self._in_progress_roots.discard(root)
            self._version += 1

    def snapshot(self) -> tuple[dict[Path, tuple[int, str]], set[Path], int]:
        with self._lock:
            return (
                dict(self._published_signatures),
                set(self._in_progress_roots),
                self._version,
            )


@dataclass(frozen=True)
class DriftGuardSession:
    telemetry: ExecutionTelemetry | None
    event_log_capture: EventLogAppendCapture | None
    generated_file_allowance: GeneratedFileDriftAllowance = field(
        default_factory=GeneratedFileDriftAllowance
    )
    runtime_publications: RuntimePublicationRegistry = field(
        default_factory=RuntimePublicationRegistry
    )
    recovery_baseline: DriftRecoveryBaseline | None = None


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
    role_label: ProviderRole
    findings_enabled: bool
    allowed_paths: set[Path]
    display: ProviderCallDisplay
    drift_session: DriftGuardSession | None = None
    generated_file_allowance: GeneratedFileDriftAllowance | None = None
    runtime_publications: RuntimePublicationRegistry | None = None
    provider_output_policy: ProviderOutputPolicy = ProviderOutputPolicy.REQUIRE_OUTPUT
    rendered_workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()
    invocation_output_file: Path | None = None
    defer_output_publication: bool = False
    protected_paths: set[Path] = field(default_factory=set)
    runtime_owned_paths: set[Path] = field(default_factory=set)
    runtime_owned_roots: set[Path] = field(default_factory=set)

    def allow_runtime_log_path(self, path: Path) -> None:
        self.allowed_paths.add(path)


@dataclass(frozen=True)
class ExecutorRoundArtifact:
    provider: ProviderRecord
    task_id: str
    content: str
    output_file: Path
    audit_round_num: int | None
    round_num: int
    output_signature: tuple[int, str] | None = None


@dataclass(frozen=True)
class ReviewerRoundArtifact:
    provider: ProviderRecord
    task_id: str
    evaluation: EvaluatedReviewResult
    output_file: Path
    audit_round_num: int | None
    round_num: int
    output_signature: tuple[int, str] | None = None


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
    invocation_output_file: Path
    output_signature: tuple[int, str]
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
    selected_round_num: int = 0


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
    selected_round_num: int = 0

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
            selected_round_num=self.selected_round_num,
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
    selected_round_num: int = 0

    def record_initial_executor_run(self, executor_run: ExecutorRoundRunResult) -> None:
        self.artifact_drift_warning_count += executor_run.drift_warning_count

    def record_initial_reviewer_run(self, reviewer_run: ReviewerRoundRunResult) -> None:
        self.artifact_drift_warning_count += reviewer_run.drift_warning_count

    def record_audit_result(self, audit_result: AuditRoundResult) -> None:
        self.invalid_candidate_round_count += audit_result.invalid_candidate_round_count
        self.no_progress_round_count += audit_result.no_progress_round_count
        self.artifact_drift_warning_count += audit_result.artifact_drift_warning_count
        self.last_round_num = audit_result.last_round_num
        if audit_result.selected_round_num > 0:
            self.selected_round_num = audit_result.selected_round_num
        self.consensus_reached = audit_result.consensus_reached
        self.continued_after_exhaustion = False

        if audit_result.latest_executor_outputs is not None:
            self.latest_executor_outputs = audit_result.latest_executor_outputs
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
    executor_prompt_workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()
    initial_review_handoff: str | None = None


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
    reviewer_prompt_workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()
    review_context_heading: str = "Current executor output(s)"
    review_context_note: str | None = None
    reviewer_instruction: str | None = None


@dataclass
class ReviewerRoundRuntime:
    reviewer_prompt: str
    invocation_semaphore: asyncio.Semaphore | None
    drift_session: DriftGuardSession
    protected_output_paths: set[Path]
    runtime_owned_paths: set[Path]
    runtime_owned_roots: set[Path]


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
    executor_prompt_workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()
    reviewer_prompt_workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()


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
    executor_prompt_workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()
    reviewer_prompt_workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()


@dataclass
class ReviewRoundState:
    reviewer_outputs: list[ReviewerRoundArtifact]
    reviewer_failure_count: int
    current_review_packet: str | None
    current_unresolved_fingerprints: tuple[str, ...]
    current_executor_fingerprint: str
