from __future__ import annotations

import pytest

from crewplane.artifacts.workspace.state.expected_set import (
    workspace_state_payloads_match_expected_set,
)
from crewplane.artifacts.workspace.state.invocations import (
    ExpectedWorkspaceInvocation,
)
from crewplane.core.workflow.keywords import ProviderRole


def test_expected_set_allows_transitive_candidate_source_payloads() -> None:
    first = _lineage_payload(1, result_commit="a" * 40, result_tree="b" * 40)
    second = _lineage_payload(
        2,
        result_commit="c" * 40,
        result_tree="d" * 40,
        source=_candidate_source(first),
    )
    third = _lineage_payload(
        3,
        result_commit="e" * 40,
        result_tree="f" * 40,
        source=_candidate_source(second),
    )

    assert workspace_state_payloads_match_expected_set(
        (first, second, third),
        (
            ExpectedWorkspaceInvocation(
                task_id="alpha",
                role=ProviderRole.EXECUTOR,
                round_num=3,
                audit_round_num=None,
            ),
        ),
    )


def _lineage_payload(
    round_num: int,
    result_commit: str,
    result_tree: str,
    source: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "node_id": "a",
        "task_id": "alpha",
        "role": "executor",
        "round_num": round_num,
        "audit_round_num": None,
        "workspace": {"lineage_producer": True},
        "result": {
            "result_commit": result_commit,
            "result_tree": result_tree,
        },
        "bundle": {
            "path": f"a/workspace-bundles/round-{round_num}.bundle",
            "sha256": str(round_num) * 64,
            "size_bytes": round_num,
        },
        "refs": {"result": f"refs/crewplane/result/{round_num}"},
    }
    if source is not None:
        payload["source"] = source
    return payload


def _candidate_source(payload: dict[str, object]) -> dict[str, object]:
    result = payload["result"]
    bundle = payload["bundle"]
    refs = payload["refs"]
    assert isinstance(result, dict)
    assert isinstance(bundle, dict)
    assert isinstance(refs, dict)
    return {
        "kind": "candidate",
        "node_id": payload["node_id"],
        "commit": result["result_commit"],
        "tree": result["result_tree"],
        "candidate_sequence": 1,
        "bundle_path": bundle["path"],
        "bundle_sha256": bundle["sha256"],
        "bundle_size_bytes": bundle["size_bytes"],
        "bundle_ref": refs["result"],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", "f" * 40),
        ("tree", "f" * 40),
        ("candidate_sequence", 2),
        ("bundle_path", "other.bundle"),
        ("bundle_sha256", "f" * 64),
        ("bundle_sha256", None),
        ("bundle_size_bytes", 2),
        ("node_id", "other"),
        ("bundle_ref", "refs/other"),
    ],
)
def test_expected_set_rejects_each_broken_candidate_link(field, value) -> None:
    first = _lineage_payload(1, "a" * 40, "b" * 40)
    source = _candidate_source(first)
    second = _lineage_payload(2, "c" * 40, "d" * 40, source)
    expected = (ExpectedWorkspaceInvocation("alpha", ProviderRole.EXECUTOR, 2, None),)
    assert workspace_state_payloads_match_expected_set((first, second), expected)
    source[field] = value
    assert not workspace_state_payloads_match_expected_set((first, second), expected)


@pytest.mark.parametrize(
    ("ref", "source_ref", "matches"),
    [
        (None, None, True),
        (17, None, False),
        (17, 17, True),
    ],
)
def test_expected_set_preserves_raw_ref_comparison(ref, source_ref, matches) -> None:
    first = _lineage_payload(1, "a" * 40, "b" * 40)
    first["refs"] = {"result": ref}
    source = _candidate_source(first)
    source["bundle_ref"] = source_ref
    second = _lineage_payload(2, "c" * 40, "d" * 40, source)
    expected = (ExpectedWorkspaceInvocation("alpha", ProviderRole.EXECUTOR, 2, None),)
    assert (
        workspace_state_payloads_match_expected_set((first, second), expected)
        is matches
    )
