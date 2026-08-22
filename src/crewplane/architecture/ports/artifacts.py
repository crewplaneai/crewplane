from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    InvocationProcessEvent,
    JsonObject,
    NodeArtifactRequest,
    VerifiedNodeArtifact,
)
from crewplane.core.execution_state import NodeState, RunManifest, RunStatus
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.workflow.keywords import ProviderRole


@dataclass(frozen=True)
class TerminalHistoryRead:
    """Result of asking an artifact integration to resolve a history path."""

    matched: bool
    path: Path | None = None
    payload: bytes | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.matched and any(
            value is not None for value in (self.path, self.payload, self.error)
        ):
            raise ValueError("An unmatched history read cannot carry a result.")
        if (
            self.matched
            and self.error is None
            and (self.path is None or self.payload is None)
        ):
            raise ValueError("A successful history read requires a path and payload.")
        if self.error is not None and self.payload is not None:
            raise ValueError("A failed history read cannot carry a payload.")


class TerminalHistoryReaderPort(Protocol):
    """Read terminal artifacts without exposing adapter storage layout to core."""

    def read_terminal_result(
        self,
        raw_path: str,
        source_root: Path,
    ) -> TerminalHistoryRead:
        """Return a terminal result, a policy error, or an unmatched result."""


@dataclass(frozen=True)
class StageFinalizeResult:
    """Summary of the consolidated artifacts produced for a workflow stage."""

    stage_name: str
    result_file: Path
    findings_file: Path | None
    included_outputs: tuple[Path, ...]
    skipped_empty_outputs: tuple[Path, ...]
    warnings: tuple[str, ...]
    generated_files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class StageTaskSpec:
    """Ordered task metadata used to finalize stage artifacts deterministically."""

    task_id: str
    role: ProviderRole
    display_name: str | None = None
    provider: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", ProviderRole(self.role))
        if self.provider is not None and not self.provider.strip():
            raise ValueError("Stage task provider cannot be blank.")


@dataclass(frozen=True)
class ProviderProcessInvocation:
    """Invocation coordinates used to persist a provider child process."""

    node_id: str
    task_id: str
    provider: str
    role: ProviderRole
    audit_round_num: int | None
    round_num: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", ProviderRole(self.role))


@dataclass(frozen=True)
class ProviderProcessPublication:
    """Trusted identity of a provider-process state publication."""

    path: Path
    signature: tuple[int, str]


@runtime_checkable
class ProviderProcessStorePort(Protocol):
    """Optional artifact capability for provider-process lifecycle records."""

    def write_provider_process_event(
        self,
        invocation: ProviderProcessInvocation,
        event: InvocationProcessEvent,
    ) -> ProviderProcessPublication:
        """Persist one provider child-process lifecycle transition."""


@runtime_checkable
class ArtifactStorePort(Protocol):
    """Runtime-facing artifact store used during a single workflow run."""

    run_id: str
    run_key_name: str
    task_name: str
    stages_dir: Path
    results_dir: Path
    logs_dir: Path
    log_cli_output: bool

    def create_node_dir(self, request: NodeArtifactRequest) -> Path:
        """Create the stage directory at the compiled locator."""

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        """Return the stage directory at the compiled locator when it exists."""

    def get_node_artifact_request(
        self,
        node_id: str,
    ) -> NodeArtifactRequest | None:
        """Return the registered compiled request for an observed node."""

    def finalize_node(
        self,
        request: NodeArtifactRequest,
        findings_enabled: bool = False,
        task_specs: tuple[StageTaskSpec, ...] = (),
        generated_file_detection_enabled: bool = True,
        generated_file_workspace_roots: dict[Path, Path | None] | None = None,
    ) -> StageFinalizeResult:
        """Finalize a node using only its compiled artifact locators."""

    def get_node_output_path(self, request: NodeArtifactRequest) -> Path:
        """Resolve the compiled output locator for a node."""

    def get_node_findings_path(self, request: NodeArtifactRequest) -> Path | None:
        """Resolve the compiled findings locator for a node, when declared."""

    def get_node_log_file(
        self,
        request: NodeArtifactRequest,
        provider: str,
        task_id: str,
        audit_round_num: int | None = None,
        round_num: int | None = None,
    ) -> Path | None:
        """Resolve a provider log below the compiled log locator."""

    def read_verified_node_artifact(
        self,
        request: NodeArtifactRequest,
        kind: str,
    ) -> VerifiedNodeArtifact:
        """Return descriptor-verified bytes and their canonical artifact path."""

    def write_node_resume_source(
        self,
        request: NodeArtifactRequest,
        payload: JsonObject,
    ) -> Path:
        """Persist resume metadata at the compiled node locator."""

    def record_hydrated_resume_node(
        self,
        node_id: str,
        source_run_id: str,
        source_run_key_name: str,
    ) -> Path:
        """Record one actually hydrated node in the running manifest."""

    def get_run_log_dir(self) -> Path:
        """Return the run-level log directory, creating it when needed."""

    def get_run_event_log_path(self) -> Path:
        """Return the run-level structured event log path."""

    def get_run_summary_path(self) -> Path:
        """Return the run-level human-readable summary path."""

    def write_preflight_plan(self, plan: PreflightExecutionPlan) -> Path:
        """Persist the successful preflight execution plan."""

    def write_preflight_static_file(self, content_ref: str, payload: bytes) -> Path:
        """Persist bundled static content read by preflight."""

    def write_preflight_manifest(self, payload: object) -> Path:
        """Persist the preflight status manifest."""

    def write_preflight_diagnostics(self, payload: object) -> Path:
        """Persist preflight diagnostics."""

    def write_preflight_metadata(self, payload: object) -> Path:
        """Persist preflight run metadata."""

    def write_preflight_summary(self, content: str) -> Path:
        """Persist the human-readable preflight summary."""

    def write_preflight_render_plan(self, payload: object) -> Path:
        """Persist successful render-plan metadata."""

    def write_preflight_execution_bundle(self, payload: object) -> Path:
        """Persist successful compiled execution-bundle metadata."""

    def write_preflight_json(self, relative_path: str, payload: object) -> Path:
        """Persist a JSON preflight artifact under the run preflight directory."""

    def write_preflight_text(self, relative_path: str, content: str) -> Path:
        """Persist a text preflight artifact under the run preflight directory."""

    def write_run_manifest(self, manifest: RunManifest) -> Path:
        """Persist the current run manifest."""

    def update_run_manifest_status(
        self,
        status: RunStatus,
        completed_at: str,
        failure_message: str | None = None,
        cancel_reason: str | None = None,
    ) -> Path:
        """Persist a terminal status update for the current run manifest."""

    def write_node_success_state(self, node_state: NodeState) -> Path:
        """Persist a successful node-boundary state record."""

    def write_workspace_export(
        self, logical_worktree_name: str, payload: object
    ) -> Path:
        """Persist a run-level workspace branch export record."""


class ArtifactAdapterPort(Protocol):
    """Factory contract for artifact storage integrations."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        """Validate and canonicalize artifact options without side effects."""

    def create_store(
        self,
        workflow_name: str,
        state_dir: Path,
        project_root: Path,
        options: JsonObject | None = None,
    ) -> ArtifactStorePort:
        """Build the artifact store for a concrete workflow run."""

    def create_terminal_history_reader(
        self,
        state_dir: Path,
        options: JsonObject | None = None,
    ) -> TerminalHistoryReaderPort:
        """Build the reader for terminal artifacts referenced during preflight."""
