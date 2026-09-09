from pathlib import Path

import pytest

from crewplane.artifacts.workspace.state.paths import (
    is_safe_workspace_stage_path,
    is_temporary_ref_evidence_name,
    is_workspace_claim_name,
    workspace_reuse_claim_filename,
    workspace_state_candidates,
    workspace_state_filename,
    workspace_temporary_refs_filename,
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


@pytest.mark.parametrize("generation", [1, 2, 100])
def test_workspace_filename_builders_preserve_names_and_categories(generation) -> None:
    state_name = workspace_state_filename("node-alpha-round1")
    assert state_name == "workspace-state-node-alpha-round1.json"
    assert is_workspace_claim_name(state_name)
    archive_name = workspace_reuse_claim_filename(Path(state_name).stem, generation)
    assert (
        archive_name
        == f"workspace-reuse-claim-workspace-state-node-alpha-round1-generation-{generation}.json"
    )
    assert is_workspace_claim_name(archive_name)
    temporary_name = workspace_temporary_refs_filename("abc123")
    assert temporary_name == "workspace-temporary-refs-abc123.json"
    assert is_temporary_ref_evidence_name(temporary_name)
    assert not is_workspace_claim_name(temporary_name)
    assert not is_temporary_ref_evidence_name(archive_name)
