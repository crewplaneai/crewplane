from __future__ import annotations

import pytest

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.invocation_identity import (
    rendered_workspace_file_invocation_id,
)


@pytest.mark.parametrize(
    ("role", "audit_round", "expected"),
    [
        (ProviderRole.EXECUTOR, None, "node.a.executor.task-b.round-2"),
        (ProviderRole.REVIEWER, None, "node.a.reviewer.task-b.round-2"),
        (ProviderRole.EXECUTOR, 3, "node.a.executor.task-b.audit-3.round-2"),
        (ProviderRole.REVIEWER, 3, "node.a.reviewer.task-b.audit-3.round-2"),
    ],
)
def test_rendered_invocation_id_preserves_persisted_format(
    role: ProviderRole, audit_round: int | None, expected: str
) -> None:
    assert (
        rendered_workspace_file_invocation_id("node.a", "task-b", role, 2, audit_round)
        == expected
    )
