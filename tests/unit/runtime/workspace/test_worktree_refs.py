from __future__ import annotations

from orchestrator_cli.runtime.workspace.worktree_refs import (
    safe_file_component,
    safe_ref_component,
)


def test_safe_ref_component_preserves_long_value_identity() -> None:
    common_prefix = "run-" + ("x" * 120)
    first = safe_ref_component(f"{common_prefix}-first")
    second = safe_ref_component(f"{common_prefix}-second")

    assert len(first) <= 96
    assert len(second) <= 96
    assert first != second


def test_safe_file_component_preserves_long_value_identity() -> None:
    common_prefix = "workspace-" + ("x" * 160)
    first = safe_file_component(f"{common_prefix}-first")
    second = safe_file_component(f"{common_prefix}-second")

    assert len(first) <= 120
    assert len(second) <= 120
    assert first != second
