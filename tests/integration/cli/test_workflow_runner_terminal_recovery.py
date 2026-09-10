from __future__ import annotations

import io
import json
import unittest
from typing import Any
from unittest.mock import patch

import pytest
from rich.console import Console

from crewplane.architecture.contracts import (
    ObserverCapabilities,
)
from crewplane.artifacts.locks import (
    LOCK_OWNER_FILENAME,
    acquire_same_context_lock,
)
from crewplane.artifacts.manager import OutputManager
from crewplane.observability import ObservabilityHub
from tests.helpers.resume_locks import FakeProcessInspector
from tests.helpers.working_directory import temporary_project_cwd
from tests.integration.cli.workflow_runner_support import (
    mock_runner_config,
    run_directories,
    run_workflow,
    runner_workflow,
)


class RequiredStopFailureObserver:
    capabilities = ObserverCapabilities(required=True)

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: object) -> None:
        del context

    def on_snapshot(self, event: object, snapshot: object) -> None:
        del event, snapshot

    def stop(self, result: object) -> None:
        del result
        raise RuntimeError("required observer stop failed")


class RequiredStopFailureHub(ObservabilityHub):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        observers = list(kwargs.pop("observers"))
        super().__init__(
            *args,
            observers=[*observers, RequiredStopFailureObserver()],
            **kwargs,
        )


class WorkflowRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_manifest_publication_failure_recovers_exact_outcome(
        self,
    ) -> None:
        with temporary_project_cwd() as root:
            console = Console(file=io.StringIO(), force_terminal=False)
            with (
                patch.object(
                    OutputManager,
                    "update_run_manifest_status",
                    side_effect=OSError("manifest publication failed"),
                ),
                pytest.raises(OSError, match="manifest publication failed"),
            ):
                await run_workflow(runner_workflow(), mock_runner_config(), console)

            run_dirs = run_directories(root)
            assert len(run_dirs) == 1
            manifest = json.loads(
                (run_dirs[0] / "manifests" / "run.json").read_text(encoding="utf-8")
            )
            assert manifest["status"] == "running"
            lock_dir = next((root / ".crewplane" / "locks").iterdir())
            owner = json.loads(
                (lock_dir / LOCK_OWNER_FILENAME).read_text(encoding="utf-8")
            )
            assert owner["terminal_recovery"] == {
                "phase": "observer_shutdown_complete",
                "status": "succeeded",
            }

            replacement = acquire_same_context_lock(
                root / ".crewplane",
                runner_workflow().name,
                owner["workflow_identity"],
                owner["workflow_signature"],
                process_inspector=FakeProcessInspector(200, "new", live=False),
            )
            try:
                recovered = json.loads(
                    (run_dirs[0] / "manifests" / "run.json").read_text(encoding="utf-8")
                )
                assert recovered["status"] == "succeeded"
                assert "cancel_reason" not in recovered
                events = [
                    json.loads(line)
                    for line in (run_dirs[0] / "logs" / "events.ndjson")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line.strip()
                ]
                terminal_event_types = [
                    event["event_type"]
                    for event in events
                    if event["event_type"]
                    in {
                        "workflow_finished",
                        "workflow_failed",
                        "workflow_cancelled",
                    }
                ]
                assert terminal_event_types == ["workflow_finished"]
                summary = (run_dirs[0] / "logs" / "summary.md").read_text(
                    encoding="utf-8"
                )
                assert "- Status: succeeded" in summary
            finally:
                replacement.release()

    async def test_required_observer_stop_failure_retains_run_lock(self) -> None:
        with temporary_project_cwd() as root:
            console = Console(file=io.StringIO(), force_terminal=False)
            with pytest.raises(RuntimeError, match="required observer stop failed"):
                await run_workflow(
                    runner_workflow(),
                    mock_runner_config(),
                    console,
                    observability_hub_cls=RequiredStopFailureHub,
                )

            run_dirs = run_directories(root)
            assert len(run_dirs) == 1
            manifest = json.loads(
                (run_dirs[0] / "manifests" / "run.json").read_text(encoding="utf-8")
            )
            assert manifest["status"] == "running"
            lock_dir = next((root / ".crewplane" / "locks").iterdir())
            owner = json.loads(
                (lock_dir / LOCK_OWNER_FILENAME).read_text(encoding="utf-8")
            )
            assert owner["terminal_recovery"] == {
                "phase": "terminal_views_published",
                "status": "succeeded",
            }

            replacement = acquire_same_context_lock(
                root / ".crewplane",
                runner_workflow().name,
                owner["workflow_identity"],
                owner["workflow_signature"],
                process_inspector=FakeProcessInspector(200, "new", live=False),
            )
            try:
                recovered = json.loads(
                    (run_dirs[0] / "manifests" / "run.json").read_text(encoding="utf-8")
                )
                assert recovered["status"] == "succeeded"
                assert "cancel_reason" not in recovered
                events = [
                    json.loads(line)
                    for line in (run_dirs[0] / "logs" / "events.ndjson")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line.strip()
                ]
                terminal_event_types = [
                    event["event_type"]
                    for event in events
                    if event["event_type"]
                    in {
                        "workflow_finished",
                        "workflow_failed",
                        "workflow_cancelled",
                    }
                ]
                assert terminal_event_types == ["workflow_finished"]
                summary = (run_dirs[0] / "logs" / "summary.md").read_text(
                    encoding="utf-8"
                )
                assert "- Status: succeeded" in summary
            finally:
                replacement.release()
