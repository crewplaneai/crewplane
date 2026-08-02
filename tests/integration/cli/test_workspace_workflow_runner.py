from __future__ import annotations

import asyncio
from pathlib import Path

from tests.helpers import isolated_git as _isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_workflow_runner import (
    run_workspace_enabled_mock_e2e,
    run_workspace_real_run_rejects_non_filesystem_artifacts,
)

isolated_git = _isolated_git_support.isolated_git


def test_workspace_enabled_mock_e2e_public_run_surfaces(
    tmp_path: Path,
    isolated_git: IsolatedGit,
) -> None:
    asyncio.run(run_workspace_enabled_mock_e2e(tmp_path, isolated_git))


def test_workspace_enabled_real_run_rejects_non_filesystem_artifacts(
    tmp_path: Path,
    isolated_git: IsolatedGit,
) -> None:
    asyncio.run(
        run_workspace_real_run_rejects_non_filesystem_artifacts(
            tmp_path,
            isolated_git,
        )
    )
