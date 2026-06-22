from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.helpers.workspace_workflow_runner import (
    local_git_supports_workspace_policy,
    run_workspace_enabled_mock_e2e,
    run_workspace_real_run_rejects_non_filesystem_artifacts,
)


def test_workspace_enabled_mock_e2e_public_run_surfaces(tmp_path: Path) -> None:
    if not local_git_supports_workspace_policy():
        pytest.skip("Git 2.34.1+ is required for workspace source policy")
    asyncio.run(run_workspace_enabled_mock_e2e(tmp_path))


def test_workspace_enabled_real_run_rejects_non_filesystem_artifacts(
    tmp_path: Path,
) -> None:
    if not local_git_supports_workspace_policy():
        pytest.skip("Git 2.34.1+ is required for workspace source policy")
    asyncio.run(run_workspace_real_run_rejects_non_filesystem_artifacts(tmp_path))
