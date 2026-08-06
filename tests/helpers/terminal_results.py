from __future__ import annotations

from pathlib import Path
from typing import Literal

from crewplane.core.execution_state import RunStatus
from tests.helpers.resume import make_run_manifest, write_run_manifest

RESULT_SOURCE_TOKEN = (
    "{{file:.crewplane/execution-results/workflow--prior-run/plan-result.md}}"
)
FINDINGS_SOURCE_TOKEN = (
    "{{file:.crewplane/execution-results/workflow--prior-run/plan-findings.md}}"
)


def write_result_source(
    project_root: Path,
    status: RunStatus = "succeeded",
    content: bytes = b"prior result",
    artifact_kind: Literal["output", "findings"] = "output",
) -> Path:
    state_dir = project_root / ".crewplane"
    run_key_name = "workflow--prior-run"
    result_relative_path = (
        "plan-result.md" if artifact_kind == "output" else "plan-findings.md"
    )
    result_path = state_dir / "execution-results" / run_key_name / result_relative_path
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(content)

    manifest = make_run_manifest(
        run_id="prior-run",
        run_key_name=run_key_name,
        status=status,
    )
    write_run_manifest(state_dir, manifest)
    return result_path
