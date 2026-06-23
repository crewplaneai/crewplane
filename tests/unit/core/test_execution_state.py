from __future__ import annotations

import pytest
from pydantic import ValidationError

from crewplane.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ArtifactDescriptor,
    NodeState,
    RunManifest,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_run_manifest, sha256_hex


def test_running_manifest_forbids_completed_at() -> None:
    payload = make_run_manifest("run", "workflow--run").model_dump(mode="json")
    payload["completed_at"] = "2026-06-09T12:00:00"

    with pytest.raises(ValidationError, match="Running manifests"):
        RunManifest.model_validate(payload)


def test_terminal_manifest_requires_completed_at() -> None:
    payload = make_run_manifest("run", "workflow--run").model_dump(mode="json")
    payload["status"] = "succeeded"

    with pytest.raises(ValidationError, match="Terminal manifests"):
        RunManifest.model_validate(payload)


def test_manifest_rejects_bad_schema_version_and_timestamp() -> None:
    payload = make_run_manifest("run", "workflow--run").model_dump(mode="json")
    payload["run_state_schema_version"] = RUN_STATE_SCHEMA_VERSION + 1

    with pytest.raises(ValidationError, match="Unsupported run state schema"):
        RunManifest.model_validate(payload)

    payload = make_run_manifest("run", "workflow--run").model_dump(mode="json")
    del payload["run_state_schema_version"]

    with pytest.raises(ValidationError, match="run_state_schema_version"):
        RunManifest.model_validate(payload)

    payload = make_run_manifest("run", "workflow--run").model_dump(mode="json")
    payload["started_at"] = "not-a-date"

    with pytest.raises(ValidationError, match="ISO 8601"):
        RunManifest.model_validate(payload)


def test_node_state_is_successful_boundary_only() -> None:
    state = NodeState(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=SCHEMA_VERSION,
        workflow_identity=".crewplane/workflows/workflow.task.md",
        workflow_name="Workflow",
        workflow_signature=sha256_hex("workflow"),
        run_id="run",
        run_key_name="workflow--run",
        node_id="build",
        completed_at="2026-06-09T12:00:00",
        artifacts=[
            ArtifactDescriptor(
                kind="output",
                relative_path="build-result.md",
                sha256=sha256_hex("result"),
                size_bytes=6,
            )
        ],
        workspace={
            "enabled": True,
            "states": [
                {
                    "workspace_state_artifact": {
                        "relative_path": "build/workspace-state.json",
                        "sha256": sha256_hex("workspace-state"),
                        "size_bytes": 100,
                    }
                }
            ],
        },
    )

    assert state.status == "succeeded"
    assert state.workspace is not None
    assert state.workspace["enabled"] is True


def test_node_state_requires_schema_marker() -> None:
    payload = {
        "plan_schema_version": SCHEMA_VERSION,
        "workflow_identity": ".crewplane/workflows/workflow.task.md",
        "workflow_name": "Workflow",
        "workflow_signature": sha256_hex("workflow"),
        "run_id": "run",
        "run_key_name": "workflow--run",
        "node_id": "build",
        "completed_at": "2026-06-09T12:00:00",
        "artifacts": [],
    }

    with pytest.raises(ValidationError, match="run_state_schema_version"):
        NodeState.model_validate(payload)


def test_artifact_descriptor_rejects_unsafe_relative_path() -> None:
    with pytest.raises(ValidationError, match="relative POSIX"):
        ArtifactDescriptor(
            kind="output",
            relative_path="../result.md",
            sha256=sha256_hex("result"),
            size_bytes=6,
        )
