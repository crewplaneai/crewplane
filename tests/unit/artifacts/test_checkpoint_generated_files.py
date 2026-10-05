from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from crewplane.artifacts.generated_files.paths import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    GENERATED_FILE_SOURCE_METADATA_NAME,
)
from crewplane.artifacts.resume import checkpoint_generated_files
from crewplane.artifacts.resume.checkpoint_files import describe_checkpoint_file
from crewplane.artifacts.resume.checkpoint_generated_files import (
    describe_generated_mapping,
    validate_generated_mappings,
)
from crewplane.core.review_checkpoint_state import (
    CheckpointGeneratedMapping,
    CheckpointInvocation,
)


@pytest.fixture
def invocation() -> CheckpointInvocation:
    return CheckpointInvocation(
        task_id="executor", role="executor", audit=2, local_round=3
    )


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "generated-files" / "snapshot"
    root.mkdir(parents=True)
    entries = []
    for name in ("z.txt", "a.txt"):
        (root / name).write_text(name)
        entries.append({"path": name, "size_bytes": len(name)})
    (root / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_text(
        json.dumps({"files": entries, "rejected_file_count": 1})
    )
    (root / GENERATED_FILE_SOURCE_METADATA_NAME).write_text(
        json.dumps({"source_root": str(tmp_path / "removed-workspace")})
    )
    return root


@pytest.mark.parametrize("empty_capture", [False, True])
def test_partial_snapshot_descriptors_and_validation_preserve_bytes(
    tmp_path: Path,
    snapshot: Path,
    invocation: CheckpointInvocation,
    empty_capture: bool,
) -> None:
    if empty_capture:
        (snapshot / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_text(
            json.dumps({"files": [], "rejected_file_count": 2})
        )
    before = {path.name: path.read_bytes() for path in snapshot.iterdir()}
    mapping, files = describe_generated_mapping(
        tmp_path, "candidate.md", snapshot, invocation
    )
    relative = "generated-files/snapshot"
    assert mapping == CheckpointGeneratedMapping(
        output_path="candidate.md", snapshot_path=relative
    )
    expected_names = [
        GENERATED_FILE_SOURCE_METADATA_NAME,
        GENERATED_FILE_SNAPSHOT_METADATA_NAME,
        *([] if empty_capture else ["a.txt", "z.txt"]),
    ]
    assert [item.relative_path for item in files] == [
        f"{relative}/{name}" for name in expected_names
    ]
    assert [item.purpose for item in files] == [
        "generated_metadata",
        "generated_metadata",
        *([] if empty_capture else ["generated_file", "generated_file"]),
    ]
    for item, name in zip(files, expected_names, strict=True):
        assert item.signature == (
            len(before[name]),
            hashlib.sha256(before[name]).hexdigest(),
        )
        assert (item.task_id, item.role, item.audit, item.local_round) == (
            "executor",
            "executor",
            2,
            3,
        )
    output = tmp_path / "candidate.md"
    output.write_text("candidate")
    descriptor = describe_checkpoint_file(
        tmp_path, "candidate.md", invocation, "executor_output"
    )
    output.unlink()
    assert validate_generated_mappings(tmp_path, [mapping], [descriptor, *files]) == {
        item.relative_path for item in files
    }
    assert not output.exists()
    assert {path.name: path.read_bytes() for path in snapshot.iterdir()} == before


def test_failed_capture_needs_no_filesystem_or_output_descriptor(
    tmp_path: Path, invocation: CheckpointInvocation
) -> None:
    root = tmp_path / "absent"
    mapping, files = describe_generated_mapping(root, "candidate.md", None, invocation)
    assert mapping == CheckpointGeneratedMapping(
        output_path="candidate.md", snapshot_path=None
    )
    assert files == []
    assert validate_generated_mappings(root, [mapping], files) == set()
    assert not root.exists()


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"{", json.JSONDecodeError),
        (b"\xff", UnicodeDecodeError),
        (b"null", ValueError),
        (b"[]", ValueError),
        (b"{}", ValueError),
        (b'{"source_root":null}', ValueError),
        (b'{"source_root":"relative"}', ValueError),
    ],
)
def test_source_metadata_errors_propagate(
    tmp_path: Path,
    snapshot: Path,
    invocation: CheckpointInvocation,
    payload: bytes,
    error: type[Exception],
) -> None:
    (snapshot / GENERATED_FILE_SOURCE_METADATA_NAME).write_bytes(payload)
    with pytest.raises(error) as caught:
        describe_generated_mapping(tmp_path, "candidate.md", snapshot, invocation)
    assert type(caught.value) is error
    if error is ValueError:
        assert (
            str(caught.value) == "Checkpoint generated-file source metadata is invalid."
        )


@pytest.mark.parametrize(
    "damage",
    [
        "source_missing",
        "source_symlink",
        "snapshot_missing",
        "snapshot_invalid",
        "size",
    ],
)
def test_unavailable_evidence_precedes_source_json_validation(
    tmp_path: Path,
    snapshot: Path,
    invocation: CheckpointInvocation,
    damage: str,
) -> None:
    source = snapshot / GENERATED_FILE_SOURCE_METADATA_NAME
    metadata = snapshot / GENERATED_FILE_SNAPSHOT_METADATA_NAME
    source.write_text("{")
    if damage == "source_missing":
        source.unlink()
    elif damage == "source_symlink":
        other = tmp_path / "source.json"
        other.write_text("{")
        source.unlink()
        source.symlink_to(other)
    elif damage == "snapshot_missing":
        metadata.unlink()
    elif damage == "snapshot_invalid":
        metadata.write_text("{")
    else:
        (snapshot / "a.txt").write_text("changed size")
    with pytest.raises(
        ValueError,
        match="^Checkpoint generated-file snapshot evidence is unavailable\\.$",
    ):
        describe_generated_mapping(tmp_path, "candidate.md", snapshot, invocation)


def test_snapshot_containment_precedes_evidence_validation(
    tmp_path: Path, snapshot: Path, invocation: CheckpointInvocation
) -> None:
    (snapshot / GENERATED_FILE_SOURCE_METADATA_NAME).write_text("{")
    with pytest.raises(ValueError, match="is not in the subpath"):
        describe_generated_mapping(
            tmp_path / "other", "candidate.md", snapshot, invocation
        )


def test_source_read_error_propagates(
    tmp_path: Path,
    snapshot: Path,
    invocation: CheckpointInvocation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Path.read_bytes
    error = OSError("source read failed")

    def read_bytes(path: Path) -> bytes:
        if path == snapshot / GENERATED_FILE_SOURCE_METADATA_NAME:
            raise error
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(OSError) as caught:
        describe_generated_mapping(tmp_path, "candidate.md", snapshot, invocation)
    assert caught.value is error


def test_generated_file_mutation_after_evidence_verification_is_rejected(
    tmp_path: Path,
    snapshot: Path,
    invocation: CheckpointInvocation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = checkpoint_generated_files.verified_generated_file_descriptors

    def verify_then_mutate(
        root: Path, require_complete_capture: bool = True
    ) -> list[tuple[str, int, str]] | None:
        entries = original(root, require_complete_capture)
        (root / "a.txt").write_text("A.txt")
        return entries

    monkeypatch.setattr(
        checkpoint_generated_files,
        "verified_generated_file_descriptors",
        verify_then_mutate,
    )
    with pytest.raises(
        ValueError,
        match="^Checkpoint dependency changed: generated-files/snapshot/a\\.txt$",
    ):
        describe_generated_mapping(tmp_path, "candidate.md", snapshot, invocation)


def test_mapping_requires_output_descriptor_before_inspecting_snapshot(
    tmp_path: Path, snapshot: Path
) -> None:
    (snapshot / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_text("{")
    mappings = [
        CheckpointGeneratedMapping(output_path="failed.md", snapshot_path=None),
        CheckpointGeneratedMapping(
            output_path="candidate.md", snapshot_path="generated-files/snapshot"
        ),
    ]
    with pytest.raises(
        ValueError, match="^Generated mapping output is not carried\\.$"
    ):
        validate_generated_mappings(tmp_path, mappings, [])


@pytest.mark.parametrize("damage", ["missing", "signature", "invocation"])
def test_mapping_dependencies_must_match_carried_descriptors(
    tmp_path: Path,
    snapshot: Path,
    invocation: CheckpointInvocation,
    damage: str,
) -> None:
    mapping, files = describe_generated_mapping(
        tmp_path, "candidate.md", snapshot, invocation
    )
    (tmp_path / "candidate.md").write_text("candidate")
    output = describe_checkpoint_file(
        tmp_path, "candidate.md", invocation, "executor_output"
    )
    if damage == "missing":
        files.pop()
    elif damage == "signature":
        files[-1] = files[-1].model_copy(update={"signature": (5, "0" * 64)})
    else:
        output = output.model_copy(update={"audit": 1})
    with pytest.raises(
        ValueError,
        match="^Generated snapshot dependencies do not match their mapping\\.$",
    ):
        validate_generated_mappings(tmp_path, [mapping], [output, *files])
