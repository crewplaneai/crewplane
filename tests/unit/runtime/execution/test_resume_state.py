import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.architecture.ports.artifacts import StageFinalizeResult
from crewplane.artifacts.manager import OutputManager
from crewplane.artifacts.naming import node_state_relative_path
from crewplane.core.file_hashing import FILE_HASH_CHUNK_BYTES
from crewplane.runtime.execution import resume
from crewplane.runtime.execution.resume import write_successful_node_state
from tests.helpers.resume import WORKFLOW_IDENTITY, make_plan


def finalized_artifacts(output: OutputManager) -> StageFinalizeResult:
    return StageFinalizeResult(
        stage_name="a",
        result_file=output.results_dir / "a-result.md",
        findings_file=output.results_dir / "a-findings.md",
        included_outputs=(),
        skipped_empty_outputs=(),
        warnings=(),
        generated_files=(output.results_dir / "a-generated-files" / "capture.bin",),
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"first\r\nsecond\r\n",
        bytes(range(256)) * (FILE_HASH_CHUNK_BYTES // 128 + 1),
    ],
    ids=["empty", "crlf", "multichunk-binary"],
)
def test_successful_node_descriptors_hash_exact_bytes_without_whole_file_reads(
    tmp_path, payload
):
    output = OutputManager("Workflow", base_dir=tmp_path)
    plan = make_plan()
    result = finalized_artifacts(output)
    paths = (result.result_file, result.findings_file, *result.generated_files)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    with patch.object(
        Path, "read_bytes", side_effect=AssertionError("Artifact must be streamed")
    ):
        state_path = write_successful_node_state(
            plan.nodes[0], plan, output, WORKFLOW_IDENTITY, result
        )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    expected = [
        {
            "kind": kind,
            "relative_path": path.relative_to(output.results_dir).as_posix(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        for kind, path in zip(
            ("output", "findings", "generated_file"), paths, strict=True
        )
    ]
    assert state["artifacts"] == expected[:2]
    assert state["generated_files"] == expected[2:]


@pytest.mark.parametrize(
    "artifact_index", [0, 1, 2], ids=["output", "findings", "generated"]
)
@pytest.mark.parametrize("failure", ["missing", "unreadable"])
def test_artifact_read_failure_prevents_successful_node_publication(
    tmp_path, monkeypatch, artifact_index, failure
):
    output = OutputManager("Workflow", base_dir=tmp_path)
    plan = make_plan()
    result = finalized_artifacts(output)
    paths = (result.result_file, result.findings_file, *result.generated_files)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"content")
    broken = paths[artifact_index]
    error = PermissionError("artifact is unreadable")
    if failure == "missing":
        broken.unlink()
    else:
        original_signature = resume.file_size_and_sha256

        def artifact_signature(path):
            if path == broken:
                raise error
            return original_signature(path)

        monkeypatch.setattr(resume, "file_size_and_sha256", artifact_signature)

    with pytest.raises(OSError) as caught:
        write_successful_node_state(
            plan.nodes[0], plan, output, WORKFLOW_IDENTITY, result
        )

    if failure == "unreadable":
        assert caught.value is error
    else:
        assert isinstance(caught.value, FileNotFoundError)
    assert not (output.stages_dir / node_state_relative_path("a")).exists()


def test_descriptor_path_error_precedes_missing_file_error(tmp_path):
    output = OutputManager("Workflow", base_dir=tmp_path)
    plan = make_plan()
    result = StageFinalizeResult("a", tmp_path / "outside.md", None, (), (), ())

    with pytest.raises(ValueError):
        write_successful_node_state(
            plan.nodes[0], plan, output, WORKFLOW_IDENTITY, result
        )

    assert not (output.stages_dir / node_state_relative_path("a")).exists()
