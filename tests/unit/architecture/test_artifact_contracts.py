from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from crewplane.architecture.contracts import (
    ArtifactContract,
    NodeArtifactRequest,
    VerifiedNodeArtifact,
    artifact_contract_for_node,
)


@pytest.mark.parametrize("locator", ("", "/absolute", "../escape", "a/../b"))
def test_artifact_contract_rejects_unsafe_locators(locator: str) -> None:
    with pytest.raises(ValidationError, match="safe relative paths"):
        ArtifactContract(output_path=locator)


def test_node_artifact_request_requires_node_and_stage_locator() -> None:
    contract = ArtifactContract(stage_path="build", output_path="build-result.md")

    with pytest.raises(ValueError, match="nonblank node_id"):
        NodeArtifactRequest(" ", contract)
    with pytest.raises(ValueError, match="stage_path"):
        NodeArtifactRequest("build", ArtifactContract(output_path="result.md"))


def test_verified_node_artifact_rejects_unverified_metadata(tmp_path: Path) -> None:
    payload = b"result"
    digest = hashlib.sha256(payload).hexdigest()

    with pytest.raises(ValueError, match="size"):
        VerifiedNodeArtifact(tmp_path / "result.md", payload, len(payload) + 1, digest)
    with pytest.raises(ValueError, match="hash"):
        VerifiedNodeArtifact(tmp_path / "result.md", payload, len(payload), "0" * 64)


def test_derived_artifact_contract_bounds_long_node_paths() -> None:
    contract = artifact_contract_for_node("node." + "x" * 300, findings_enabled=True)

    assert contract.stage_path is not None
    assert contract.findings_path is not None
    assert len(contract.stage_path) <= 180
    assert len(contract.output_path) <= 180
    assert len(contract.findings_path) <= 180
    assert contract.log_path == f"{contract.stage_path}/logs"
