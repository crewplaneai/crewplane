from __future__ import annotations

import json
from pathlib import Path

from tests.helpers.workspace_workflow_fixtures import resumed_succeeded_run


def test_resumed_succeeded_run_selects_manifest_with_resume_identity(
    tmp_path: Path,
) -> None:
    stages_root = tmp_path / ".crewplane" / "execution-stages"
    resumed_run = stages_root / "workflow-a-resumed"
    unrelated_run = stages_root / "workflow-z-unrelated"
    for run_dir, resume_source_run_id in (
        (resumed_run, "failed-run-id"),
        (unrelated_run, None),
    ):
        manifests_dir = run_dir / "manifests"
        manifests_dir.mkdir(parents=True)
        (manifests_dir / "run.json").write_text(
            json.dumps(
                {
                    "status": "succeeded",
                    "resume_source_run_id": resume_source_run_id,
                }
            ),
            encoding="utf-8",
        )

    assert resumed_succeeded_run(tmp_path) == resumed_run
