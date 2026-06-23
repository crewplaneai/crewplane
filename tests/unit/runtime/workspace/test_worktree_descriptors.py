from __future__ import annotations

import json
from pathlib import Path

from crewplane.runtime.workspace.worktree.descriptors import (
    load_source_ref_from_state,
)


def test_load_source_ref_from_state_rejects_bool_integer_fields(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "implement" / "workspace-state.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "role": "executor",
                "node_id": "implement",
                "workspace": {"lineage_producer": True},
                "result": {
                    "result_commit": "a" * 40,
                    "result_tree": "b" * 40,
                },
                "bundle": {
                    "path": "workspace.bundle",
                    "sha256": "c" * 64,
                    "size_bytes": True,
                },
                "source": {
                    "kind": "node",
                    "node_id": "plan",
                    "commit": "d" * 40,
                    "tree": "e" * 40,
                    "candidate_sequence": True,
                    "bundle_path": "plan/workspace.bundle",
                    "bundle_sha256": "f" * 64,
                    "bundle_size_bytes": False,
                },
            }
        ),
        encoding="utf-8",
    )

    source_ref = load_source_ref_from_state(state_path)

    assert source_ref.bundle_size_bytes is None
    assert source_ref.upstream_sources[0].candidate_sequence is None
    assert source_ref.upstream_sources[0].bundle_size_bytes is None
