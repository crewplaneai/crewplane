from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from crewplane.artifacts.run_history import RunHistoryError, find_same_context_runs
from crewplane.core.config import Config
from crewplane.core.preflight.source import PreflightWorkflowSource

from .. import workflow_runner
from ..run.resume import workflow_identity_for_source

MOCK_INVOKER_RESOLVED_IDENTITY = "crewplane.adapters.invokers.mock:MockInvokerAdapter"


@dataclass(frozen=True)
class MockRunEvidence:
    found: bool
    warning: str | None = None


def find_successful_mock_run_evidence(
    project_root: Path,
    state_dir: Path,
    config: Config,
    source: PreflightWorkflowSource,
    console: Console,
) -> MockRunEvidence:
    try:
        preview = workflow_runner.compile_workflow_preview(
            config=config,
            source=source,
            console=console,
            no_live=True,
            fingerprint_key_policy="read_only",
            project_root=project_root,
            state_dir=state_dir,
            check_cli_availability=False,
            which_fn=None,
            workspace_real_execution=False,
        )
        if preview.has_errors():
            return MockRunEvidence(False, "Default workflow preflight has errors.")
        if preview.workflow_name is None or preview.workflow_signature is None:
            return MockRunEvidence(False, "Default workflow identity is unavailable.")
        workflow_identity = workflow_identity_for_source(source, project_root)
        records = find_same_context_runs(
            state_dir,
            workflow_identity,
            preview.workflow_name,
            preview.workflow_signature,
        )
    except (OSError, RunHistoryError, ValueError) as exc:
        return MockRunEvidence(False, str(exc))
    return MockRunEvidence(
        any(record_is_successful_mock_run(record) for record in records)
    )


def record_is_successful_mock_run(record: object) -> bool:
    manifest = getattr(record, "manifest", None)
    if getattr(manifest, "status", None) != "succeeded":
        return False
    snapshot = getattr(manifest, "runtime_config_snapshot", None)
    if not isinstance(snapshot, dict):
        return False
    invoker = snapshot.get("invoker")
    return (
        isinstance(invoker, dict)
        and invoker.get("resolved_identity") == MOCK_INVOKER_RESOLVED_IDENTITY
    )


__all__ = [
    "MOCK_INVOKER_RESOLVED_IDENTITY",
    "MockRunEvidence",
    "find_successful_mock_run_evidence",
    "record_is_successful_mock_run",
]
