from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from crewplane.architecture.contracts import ArtifactContract, NodeArtifactRequest
from crewplane.artifacts import OutputManager
from crewplane.artifacts.naming import build_node_state_filename
from crewplane.artifacts.verification import read_verified_node_artifact
from crewplane.core.execution_state import ArtifactDescriptor
from tests.helpers.resume import make_node_state, make_run_manifest, write_node_state


def test_read_verified_node_artifact_reads_output_and_findings(tmp_path: Path) -> None:
    stages_dir = tmp_path / "execution-stages"
    results_dir = tmp_path / "execution-results"

    request = _request(
        "build-result.md",
        findings_path="build-findings.md",
    )
    output_payload = b"build output"
    findings_payload = b"build findings"
    output_descriptor = _write_artifact(
        results_dir,
        request.contract.output_path,
        output_payload,
        "output",
    )
    findings_descriptor = _write_artifact(
        results_dir,
        request.contract.findings_path,
        findings_payload,
        "findings",
    )
    _write_node_state(
        stages_dir,
        request,
        [output_descriptor, findings_descriptor],
    )

    output = read_verified_node_artifact(stages_dir, results_dir, request, "output")
    findings = read_verified_node_artifact(stages_dir, results_dir, request, "findings")

    assert output.path == results_dir / request.contract.output_path
    assert output.payload == output_payload
    assert findings.path == results_dir / request.contract.findings_path
    assert findings.payload == findings_payload


def test_read_verified_node_artifact_rejects_missing_state_descriptor(
    tmp_path: Path,
) -> None:
    request = _request("build-result.md")

    with pytest.raises(ValueError, match="no valid successful state descriptor"):
        read_verified_node_artifact(tmp_path, tmp_path / "results", request, "output")


def test_read_verified_node_artifact_rejects_corrupt_state_descriptor(
    tmp_path: Path,
) -> None:
    stages_dir = tmp_path / "execution-stages"
    results_dir = tmp_path / "execution-results"
    request = _request("build-result.md")
    _write_corrupt_node_state(stages_dir, request.node_id)

    with pytest.raises(ValueError, match="no valid successful state descriptor"):
        read_verified_node_artifact(stages_dir, results_dir, request, "output")


def test_read_verified_node_artifact_rejects_locator_not_in_state(
    tmp_path: Path,
) -> None:
    stages_dir = tmp_path / "execution-stages"
    results_dir = tmp_path / "execution-results"
    request = _request("build-result.md")
    _write_artifact(
        results_dir, request.contract.output_path, b"build output", "output"
    )
    mismatched = ArtifactDescriptor(
        kind="output",
        relative_path="other-output.md",
        sha256=hashlib.sha256(b"build output").hexdigest(),
        size_bytes=len(b"build output"),
    )
    _write_node_state(stages_dir, request, [mismatched])

    with pytest.raises(ValueError, match="does not match its plan"):
        read_verified_node_artifact(stages_dir, results_dir, request, "output")


def test_read_verified_node_artifact_rejects_missing_artifact_file(
    tmp_path: Path,
) -> None:
    stages_dir = tmp_path / "execution-stages"
    results_dir = tmp_path / "execution-results"
    request = _request("build-result.md")
    descriptor = ArtifactDescriptor(
        kind="output",
        relative_path=request.contract.output_path,
        sha256=hashlib.sha256(b"build output").hexdigest(),
        size_bytes=len(b"build output"),
    )
    _write_node_state(stages_dir, request, [descriptor])

    with pytest.raises(ValueError, match="artifact is unavailable"):
        read_verified_node_artifact(stages_dir, results_dir, request, "output")


def test_read_verified_node_artifact_rejects_hash_mismatch(tmp_path: Path) -> None:
    stages_dir = tmp_path / "execution-stages"
    results_dir = tmp_path / "execution-results"
    request = _request("build-result.md")
    payload = b"expected output"
    _write_artifact(results_dir, request.contract.output_path, payload, "output")
    wrong_descriptor = ArtifactDescriptor(
        kind="output",
        relative_path=request.contract.output_path,
        sha256=hashlib.sha256(b"corrupt").hexdigest(),
        size_bytes=len(payload),
    )
    _write_node_state(stages_dir, request, [wrong_descriptor])

    with pytest.raises(ValueError, match="bytes do not match state"):
        read_verified_node_artifact(stages_dir, results_dir, request, "output")


def test_read_verified_node_artifact_rejects_size_mismatch(tmp_path: Path) -> None:
    stages_dir = tmp_path / "execution-stages"
    results_dir = tmp_path / "execution-results"
    request = _request("build-result.md")
    payload = b"expected output"
    descriptor = _write_artifact(
        results_dir,
        request.contract.output_path,
        payload,
        "output",
    )
    wrong_descriptor = ArtifactDescriptor(
        kind=descriptor.kind,
        relative_path=descriptor.relative_path,
        sha256=descriptor.sha256,
        size_bytes=descriptor.size_bytes + 1,
    )
    _write_node_state(stages_dir, request, [wrong_descriptor])

    with pytest.raises(ValueError, match="bytes do not match state"):
        read_verified_node_artifact(stages_dir, results_dir, request, "output")


def test_read_verified_node_artifact_rejects_missing_findings_locator(
    tmp_path: Path,
) -> None:
    request = _request("build-result.md")

    with pytest.raises(ValueError, match="has no findings artifact locator"):
        read_verified_node_artifact(tmp_path, tmp_path / "results", request, "findings")


def test_read_verified_node_artifact_rejects_unsupported_kind(tmp_path: Path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)

    with pytest.raises(ValueError, match="Unsupported node artifact kind 'invalid'"):
        output.read_verified_node_artifact(_request("build-result.md"), "invalid")


def _request(output_path: str, findings_path: str | None = None) -> NodeArtifactRequest:
    return NodeArtifactRequest(
        "build",
        ArtifactContract(
            stage_path="build",
            output_path=output_path,
            findings_path=findings_path,
            log_path="build/logs",
            result_path=output_path,
        ),
    )


def _write_artifact(
    results_dir: Path,
    relative_path: str,
    payload: bytes,
    kind: str,
) -> ArtifactDescriptor:
    path = results_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return ArtifactDescriptor(
        kind=kind,
        relative_path=relative_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def _write_node_state(
    stages_dir: Path,
    request: NodeArtifactRequest,
    artifacts: list[ArtifactDescriptor],
) -> None:
    manifest = make_run_manifest(run_id="run", run_key_name="workflow--run")
    write_node_state(stages_dir, make_node_state(manifest, request.node_id, artifacts))


def _write_corrupt_node_state(stages_dir: Path, node_id: str) -> None:
    state_path = stages_dir / "manifests" / "nodes" / build_node_state_filename(node_id)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{", encoding="utf-8")
