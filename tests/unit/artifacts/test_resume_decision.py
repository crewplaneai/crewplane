from __future__ import annotations

from pathlib import Path

from orchestrator_cli.artifacts.resume.decision import decide_same_context_action
from orchestrator_cli.artifacts.run_history import RunHistoryRecord
from tests.helpers.resume import make_run_manifest


def record(run_id: str, status: str, started_offset: int) -> RunHistoryRecord:
    manifest = make_run_manifest(
        run_id,
        f"workflow--{run_id}",
        status=status,
        started_offset=started_offset,
    )
    return RunHistoryRecord(
        manifest=manifest,
        manifest_path=Path(f"/tmp/{run_id}/manifests/run.json"),
        run_dir=Path(f"/tmp/{run_id}"),
        results_dir=Path(f"/tmp/results/{run_id}"),
    )


def test_success_first_skip_ignores_newer_failed_run() -> None:
    decision = decide_same_context_action(
        (
            record("newer-failure", "failed", 10),
            record("older-success", "succeeded", 0),
        ),
        force=False,
    )

    assert decision.kind == "skip"
    assert decision.successful_run is not None
    assert decision.successful_run.manifest.run_id == "older-success"


def test_newest_failed_or_cancelled_run_becomes_resume_source() -> None:
    decision = decide_same_context_action(
        (
            record("newer-cancelled", "cancelled", 10),
            record("older-failed", "failed", 0),
        ),
        force=False,
    )

    assert decision.kind == "resume"
    assert decision.resume_source is not None
    assert decision.resume_source.manifest.run_id == "newer-cancelled"


def test_force_executes_full_run() -> None:
    decision = decide_same_context_action((record("success", "succeeded", 0),), True)

    assert decision.kind == "execute_full"
