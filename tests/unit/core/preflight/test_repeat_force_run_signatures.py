from copy import deepcopy
from pathlib import Path

from crewplane.artifacts.naming import build_lock_name
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.core.preflight.plan_signatures import semantic_workflow_payload
from tests.unit.core.preflight.workspace_preflight_signatures_support import (
    compile_source_with_source_snapshot,
    workspace_signature_config,
)


def test_only_top_level_repetition_is_excluded_without_mutating_source() -> None:
    payload = {
        "repeat_force_run_count": 3,
        "nodes": [{"review_starts_with": "executor", "repeat_force_run_count": 4}],
        "worktrees": {"a": {"kind": "worktree", "create_branch": True}},
    }
    original = deepcopy(payload)
    assert semantic_workflow_payload(payload) == {
        "nodes": [{"repeat_force_run_count": 4}],
        "worktrees": {"a": {"kind": "worktree"}},
    }
    assert payload == original


def test_count_changes_preserve_compiled_signature_and_lock_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflow.task.md"
    config = workspace_signature_config({"enabled": False})
    signatures = []
    for count in [None, 1, 3, None]:
        field = f"repeat_force_run_count: {count}\n" if count is not None else ""
        path.write_text(
            f"---\nname: Repeat\n{field}nodes:\n"
            "  - id: scan\n    mode: sequential\n    providers: [alpha]\n"
            "---\n## scan\nScan.\n",
            encoding="utf-8",
        )
        source = load_workflow_source_for_preflight(path, tmp_path)
        preview = compile_source_with_source_snapshot(tmp_path, source, None, config)
        assert not preview.has_errors()
        assert preview.workflow_signature is not None
        signatures.append(preview.workflow_signature)
    assert len(set(signatures)) == 1
    assert (
        len({build_lock_name("Repeat", "identity", value) for value in signatures}) == 1
    )
