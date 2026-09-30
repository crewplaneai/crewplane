from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from crewplane.artifacts.workspace.state.source_fields import invocation_source_payload
from crewplane.core.preflight.models import WorkspaceSelectionRecord
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.worktree.descriptors import (
    load_source_ref_from_state,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.workspace_lineage_bundles import create_result_bundle
from tests.helpers.workspace_service import (
    create_git_repo,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
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


@pytest.mark.parametrize("kind", ["project", "node", "candidate"])
def test_descriptor_reconstruction_preserves_runtime_source_kinds(tmp_path, kind):
    payload = valid_worktree_payload()
    if kind != "project":
        payload["source"] = {
            "kind": kind,
            "node_id": "upstream",
            "commit": "f" * 40,
            "tree": "b" * 40,
            "candidate_sequence": 1,
            "bundle_path": "upstream/result.bundle",
            "bundle_sha256": "e" * 64,
            "bundle_size_bytes": 42,
            "bundle_ref": "refs/crewplane/test/result",
            "upstream_sources": [payload["source"]],
        }
    payload["invocation_source"] = invocation_source_payload(payload["source"])
    state_path = tmp_path / "build" / "workspace-state.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    source = load_source_ref_from_state(state_path).upstream_sources[0]
    assert source.source_kind == kind
    assert source.source_node_id == (None if kind == "project" else "upstream")
    assert source.candidate_sequence == (None if kind == "project" else 1)
    if kind != "project":
        assert source.bundle_path == tmp_path / "upstream/result.bundle"
        assert source.bundle_sha256 == "e" * 64
        assert source.bundle_size_bytes == 42
        assert source.bundle_ref == "refs/crewplane/test/result"
        assert source.upstream_sources[0].source_kind == "project"


@pytest.mark.parametrize("kind", ["unknown", None])
def test_descriptor_reconstruction_rejects_invalid_source_kind(tmp_path, kind):
    payload = valid_worktree_payload()
    payload["source"]["kind"] = kind
    payload["invocation_source"]["source_kind"] = kind
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid source kind"):
        load_source_ref_from_state(state_path)


def test_authored_source_policy_rejects_runtime_candidate_kind():
    with pytest.raises(ValidationError, match="source_kind"):
        WorkspaceSelectionRecord(source_kind="candidate")


@pytest.mark.parametrize("kind", ["project", "node", "candidate"])
def test_prepared_workspace_context_preserves_runtime_source_kind(tmp_path, kind):
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", True, kind="worktree")
    source = plan.workspace_source
    assert source is not None
    project = WorktreeSourceRef(
        "project", None, source.run_base_commit, source.source_tree
    )
    source_ref = project
    if kind != "project":
        commit, tree, ref, bundle, digest = create_result_bundle(
            tmp_path, repo, "upstream"
        )
        source_ref = WorktreeSourceRef(
            source_kind=kind,
            source_node_id="upstream",
            source_commit=commit,
            source_tree=tree,
            candidate_sequence=1,
            bundle_path=bundle,
            bundle_sha256=digest,
            bundle_size_bytes=bundle.stat().st_size,
            bundle_ref=ref,
            upstream_sources=(project,),
        )
    output = workspace_output_manager(tmp_path, repo)
    with patch(
        "crewplane.runtime.workspace.service.worktree.invocation_source_ref",
        return_value=source_ref,
    ):
        prepared = prepare_invocation_workspace(
            workspace_invocation_request(plan, output), workspace_invocation_context()
        )
    try:
        context = prepared.invocation_context.workspace
        assert context is not None
        assert context.invocation_source.source_kind == kind
        assert context.invocation_source.source_node_id == source_ref.source_node_id
        assert context.invocation_source.source_commit == source_ref.source_commit
        assert context.invocation_source.source_tree == source_ref.source_tree
        assert (
            context.invocation_source.candidate_sequence
            == source_ref.candidate_sequence
        )
    finally:
        prepared.mark_cancelled("Test completed.")
