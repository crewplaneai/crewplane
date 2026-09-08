from pathlib import Path

import pytest

from crewplane.artifacts.workspace.state.paths import (
    is_safe_workspace_stage_path,
    is_temporary_ref_evidence_name,
    is_workspace_claim_name,
    workspace_state_candidates,
)


@pytest.mark.parametrize(
    ("name", "claim", "temporary"),
    [
        ("workspace-state.json", True, False),
        ("workspace-state-node-round1.json", True, False),
        ("workspace-reuse-claim-node.json", True, False),
        ("workspace-temporary-refs-abc.json", False, True),
        ("workspace-state.json.bak", False, False),
        ("workspace-state-node.txt", False, False),
        ("workspace-reuse-claim-node.txt", False, False),
        ("workspace-temporary-refs-abc.txt", False, False),
        ("workspace-stateful.json", False, False),
        ("other.json", False, False),
    ],
)
def test_workspace_evidence_name_contract(
    name: str, claim: bool, temporary: bool
) -> None:
    assert is_workspace_claim_name(name) is claim
    assert is_temporary_ref_evidence_name(name) is temporary


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("implement", True),
        ("imported/implement", True),
        ("imported/logs", True),
        ("", False),
        (" ", False),
        (".", False),
        ("./", False),
        ("/absolute", False),
        ("../outside", False),
        ("nested/../outside", False),
        ("logs", False),
        ("logs/nested", False),
        ("manifests/node", False),
        ("workspace-exports", False),
    ],
)
def test_workspace_stage_path_contract(value: str, valid: bool) -> None:
    assert is_safe_workspace_stage_path(value) is valid


def test_workspace_state_candidates_preserve_order_and_defer_safety_checks(
    tmp_path: Path,
) -> None:
    (tmp_path / "workspace-state-z.json").write_text("{}")
    (tmp_path / "workspace-state-a.json").mkdir()
    (tmp_path / "workspace-reuse-claim-a.json").write_text("{}")
    (tmp_path / "workspace-state-a.txt").write_text("{}")

    assert workspace_state_candidates(tmp_path) == (
        tmp_path / "workspace-state.json",
        tmp_path / "workspace-state-a.json",
        tmp_path / "workspace-state-z.json",
    )
