from __future__ import annotations

from copy import deepcopy

import pytest

from crewplane.artifacts.workspace.state.contracts import (
    PersistedWorkspaceOperation,
    workspace_state_contract_errors,
)
from tests.unit.artifacts.workspace_state_contracts_support import (
    valid_snapshot_payload,
    valid_worktree_payload,
)


@pytest.mark.parametrize("operation", ["cleanup", "failed_invocation"])
def test_cleanup_contract_rejects_unresolved_failed_process_liveness(
    operation: PersistedWorkspaceOperation,
) -> None:
    payload = valid_worktree_payload()
    payload["status"] = "failed"
    payload["process_drain"] = {"status": "unresolved", "pid": 42}

    errors = workspace_state_contract_errors(payload, operation)

    assert f"{operation} workspace has unresolved process liveness" in errors


@pytest.mark.parametrize("status", ["succeeded", "cancelled", "planned"])
def test_failed_invocation_contract_requires_failed_status(status: str) -> None:
    payload = valid_worktree_payload()
    payload["status"] = status

    errors = workspace_state_contract_errors(payload, "failed_invocation")

    assert "failed_invocation requires a failed workspace" in errors


@pytest.mark.parametrize(
    "operation",
    [
        "materialization",
        "resume",
        "duplicate_skip",
        "rendering",
        "export",
        "cleanup",
        "ref_cleanup",
    ],
)
def test_succeeded_workspace_contract_rejects_unresolved_workspace_mutator(
    operation: PersistedWorkspaceOperation,
) -> None:
    payload = valid_worktree_payload()
    payload["workspace_mutator"] = {
        "status": "unresolved",
        "operation": "success_finalizer",
    }

    errors = workspace_state_contract_errors(payload, operation)

    assert "successful workspace has unresolved workspace mutator" in errors


@pytest.mark.parametrize("operation", ["cleanup", "ref_cleanup", "failed_invocation"])
def test_cleanup_contract_rejects_unresolved_failed_workspace_mutator(
    operation: PersistedWorkspaceOperation,
) -> None:
    payload = valid_worktree_payload()
    payload["status"] = "failed"
    payload["workspace_mutator"] = {
        "status": "unresolved",
        "operation": "retry_reset",
    }

    errors = workspace_state_contract_errors(payload, operation)

    assert f"{operation} workspace has unresolved workspace mutator" in errors


def test_materialization_contract_accepts_running_workspace_mutator() -> None:
    payload = valid_worktree_payload()
    payload["status"] = "running"
    payload["workspace_mutator"] = {
        "status": "unresolved",
        "operation": "success_finalizer",
    }

    errors = workspace_state_contract_errors(payload, "materialization")

    assert "unresolved workspace mutator" not in "; ".join(errors)


@pytest.mark.parametrize(
    ("evidence", "expected_error"),
    [
        ("unresolved", "workspace mutator evidence is invalid"),
        (
            {"status": "unknown", "operation": "success_finalizer"},
            "workspace mutator status is invalid",
        ),
        (
            {"status": "confirmed", "operation": "", "outcome": "finished"},
            "workspace mutator operation is invalid",
        ),
        (
            {"status": "confirmed", "operation": "retry_reset"},
            "confirmed workspace mutator outcome is invalid",
        ),
    ],
)
def test_workspace_contract_rejects_malformed_workspace_mutator_evidence(
    evidence: object,
    expected_error: str,
) -> None:
    payload = valid_worktree_payload()
    payload["workspace_mutator"] = evidence

    errors = workspace_state_contract_errors(payload, "resume")

    assert expected_error in errors


def test_cleanup_contract_accepts_removed_publication_after_capture_failure() -> None:
    payload = valid_worktree_payload()
    payload["status"] = "failed"
    payload.pop("result")
    payload.pop("refs")
    payload.pop("bundle")
    publication = payload["ref_publication"]
    assert isinstance(publication, dict)
    publication["phase"] = "removed"

    errors = workspace_state_contract_errors(payload, "cleanup")

    assert errors == ()


def test_cleanup_contract_rejects_publication_on_ordinary_non_lineage_state() -> None:
    payload = valid_snapshot_payload()
    payload["ref_publication"] = deepcopy(valid_worktree_payload()["ref_publication"])

    errors = workspace_state_contract_errors(payload, "ref_cleanup")

    assert "non-lineage workspace has ref publication evidence" in errors
