from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.runtime.workspace.worktree.descriptors import (
    load_source_ref_from_state,
)
from tests.unit.artifacts.workspace_state_contracts_support import (
    valid_worktree_payload,
)


@pytest.mark.parametrize("bundle_size", [0, 42])
def test_load_valid_lineage_preserves_bundle_size_and_source(tmp_path, bundle_size):
    payload = valid_worktree_payload()
    payload["bundle"]["size_bytes"] = bundle_size
    state_path = tmp_path / "build" / "workspace-state.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    source = load_source_ref_from_state(state_path)

    assert source.source_commit == "d" * 40
    assert source.source_tree == "b" * 40
    assert source.bundle_size_bytes == bundle_size
    assert type(source.bundle_size_bytes) is int
    assert source.bundle_path == tmp_path / "build/workspace-bundles/result.bundle"
    assert source.bundle_sha256 == "e" * 64
    assert source.candidate_sequence == 1
    assert len(source.upstream_sources) == 1
    upstream = source.upstream_sources[0]
    assert upstream.source_kind == "project"
    assert upstream.source_commit == "a" * 40
    assert upstream.candidate_sequence is None
    assert upstream.bundle_size_bytes is None


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

    with pytest.raises(RuntimeError, match="lacks required hardening evidence"):
        load_source_ref_from_state(state_path)
