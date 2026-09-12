from __future__ import annotations

from crewplane.artifacts.workspace.state.contracts import (
    workspace_state_contract_errors,
)
from crewplane.artifacts.workspace.state.source_fields import (
    invocation_source_payload,
    source_field_mismatches,
)
from tests.unit.artifacts.workspace_state_contracts_support import (
    valid_worktree_payload,
)


def test_projection_omits_unavailable_bundle_fields_and_recursive_sources() -> None:
    source = {
        "kind": "candidate",
        "node_id": "build",
        "commit": "a" * 40,
        "tree": "b" * 40,
        "candidate_sequence": 0,
        "bundle_sha256": "c" * 64,
        "bundle_size_bytes": 0,
        "upstream_sources": [{"kind": "project"}],
    }
    assert invocation_source_payload(source) == {
        "source_kind": "candidate",
        "source_node_id": "build",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "candidate_sequence": 0,
        "source_bundle_sha256": "c" * 64,
        "source_bundle_size_bytes": 0,
    }


def test_source_comparison_preserves_absent_and_null_equivalence() -> None:
    assert source_field_mismatches({"node_id": None, "bundle_ref": None}, {}) == ()
    assert (
        source_field_mismatches({}, {"source_tree": None, "candidate_sequence": None})
        == ()
    )
    assert invocation_source_payload({"bundle_ref": None}) == {
        "source_bundle_ref": None
    }


def test_invocation_source_diagnostics_preserve_labels_and_order() -> None:
    payload = valid_worktree_payload()
    payload["invocation_source"] = {
        "source_kind": "other",
        "source_node_id": "other",
        "source_commit": "other",
        "source_tree": "other",
        "candidate_sequence": "other",
        "source_bundle_path": "other",
        "source_bundle_sha256": "other",
        "source_bundle_size_bytes": "other",
        "source_bundle_ref": "other",
    }
    assert workspace_state_contract_errors(payload, "resume") == (
        "invocation source source_kind mismatch",
        "invocation source source_node_id mismatch",
        "invocation source source_commit mismatch",
        "invocation source source_tree mismatch",
        "invocation source candidate_sequence mismatch",
        "invocation source bundle_path mismatch",
        "invocation source bundle_sha256 mismatch",
        "invocation source bundle_size_bytes mismatch",
        "invocation source bundle_ref mismatch",
    )
