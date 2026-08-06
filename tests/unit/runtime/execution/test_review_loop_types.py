from __future__ import annotations

from pathlib import Path

from crewplane.runtime.execution.review_loop.types import GeneratedFileDriftAllowance


def test_generated_file_drift_allowance_preserves_snapshot_state() -> None:
    allowance = GeneratedFileDriftAllowance()
    unchanged_allowance = GeneratedFileDriftAllowance()
    snapshot_root = Path("generated-files")
    generated_file = snapshot_root / "src" / "app.py"
    published_signatures = {generated_file: (12, "sha256")}

    assert allowance.snapshot() == ({}, set(), 0)
    assert allowance == unchanged_allowance

    allowance.start_snapshot(snapshot_root)

    assert allowance.snapshot() == ({}, {snapshot_root}, 1)
    assert allowance != unchanged_allowance

    allowance.finish_snapshot(snapshot_root, published_signatures)

    assert allowance.snapshot() == (published_signatures, set(), 2)
