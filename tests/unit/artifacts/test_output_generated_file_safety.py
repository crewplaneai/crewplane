from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.generated_files.catalog import (
    generated_file_snapshot_rejection_summary,
    snapshot_generated_file_workspace,
)
from tests.helpers.artifacts import node_artifact_request


def test_workspace_generated_file_snapshot_bounds_rejection_details(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    source_dir = workspace / "src"
    source_dir.mkdir(parents=True)
    generated_files = []
    for index in range(6):
        generated_file = source_dir / f"{index}.txt"
        generated_file.write_text(str(index), encoding="utf-8")
        generated_files.append(generated_file)
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"

    with (
        patch(
            "crewplane.artifacts.generated_files.catalog."
            "MAX_GENERATED_FILE_SNAPSHOT_FILES",
            1,
        ),
        patch(
            "crewplane.artifacts.generated_files.catalog."
            "MAX_GENERATED_FILE_SNAPSHOT_REJECTION_DETAILS",
            2,
        ),
    ):
        snapshot_root = snapshot_generated_file_workspace(
            alpha_output,
            workspace,
            candidate_files=generated_files,
        )

    metadata = json.loads(
        (snapshot_root / ".crewplane-generated-file-snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    summary = generated_file_snapshot_rejection_summary(snapshot_root)

    assert len(metadata["files"]) == 1
    assert metadata["rejected_file_count"] == 5
    assert len(metadata["rejected_files"]) == 2
    assert metadata["rejected_files_truncated"]
    assert summary.total_count == 5
    assert len(summary.recorded_files) == 2
    assert summary.truncated


def test_workspace_generated_file_snapshot_records_size_growth_during_copy(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.txt").write_text("x", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")

    def expanded_source(
        descriptor: int,
        mode: str,
        closefd: bool = True,
    ) -> io.BytesIO:
        assert descriptor >= 0
        assert mode == "rb"
        assert not closefd
        return io.BytesIO(b"expanded")

    with patch(
        "crewplane.artifacts.generated_files.snapshot_io.os.fdopen",
        new=expanded_source,
    ):
        snapshot_root = snapshot_generated_file_workspace(alpha_output, workspace)

    metadata = json.loads(
        (snapshot_root / ".crewplane-generated-file-snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["files"] == []
    assert metadata["rejected_files"][0]["reason"] == "copy_failed"
    assert metadata["rejected_files"][0]["path"] == "src/app.txt"
    assert not (snapshot_root / "src" / "app.txt").exists()


def test_workspace_generated_file_snapshot_removes_truncated_copy(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.txt").write_text(
        "complete",
        encoding="utf-8",
    )
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")

    def truncated_source(
        descriptor: int,
        mode: str,
        closefd: bool = True,
    ) -> io.BytesIO:
        assert descriptor >= 0
        assert mode == "rb"
        assert not closefd
        return io.BytesIO(b"par")

    with patch(
        "crewplane.artifacts.generated_files.snapshot_io.os.fdopen",
        new=truncated_source,
    ):
        snapshot_root = snapshot_generated_file_workspace(
            alpha_output,
            workspace,
        )

    metadata = json.loads(
        (snapshot_root / ".crewplane-generated-file-snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["files"] == []
    assert metadata["rejected_files"][0]["reason"] == "copy_failed"
    assert not (snapshot_root / "src" / "app.txt").exists()


def test_workspace_generated_file_snapshot_ignores_hardlinked_files(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    outside_file = base_dir / "outside.txt"
    outside_file.write_text("external", encoding="utf-8")
    generated_file = workspace / "src" / "leak.txt"
    try:
        os.link(outside_file, generated_file)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text("Updated `src/leak.txt`.\n", encoding="utf-8")

    snapshot = snapshot_generated_file_workspace(alpha_output, workspace)

    assert not (snapshot / "src" / "leak.txt").exists()


def test_workspace_generated_file_snapshot_rejects_symlink_swap_during_copy(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    source = workspace / "src" / "app.txt"
    source.write_text("inside", encoding="utf-8")
    outside = base_dir / "outside.txt"
    outside.write_text("escape", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")
    original_open = os.open
    swapped = False

    def swap_before_source_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "app.txt" and dir_fd is not None and not swapped:
            source.unlink()
            try:
                source.symlink_to(outside)
            except OSError as exc:
                pytest.skip(f"symlinks are unavailable: {exc}")
            swapped = True
        if dir_fd is None:
            return original_open(path, flags)
        return original_open(path, flags, dir_fd=dir_fd)

    with patch(
        "crewplane.artifacts.generated_files.snapshot_io.os.open",
        new=swap_before_source_open,
    ):
        snapshot_root = snapshot_generated_file_workspace(
            alpha_output,
            workspace,
        )

    metadata = json.loads(
        (snapshot_root / ".crewplane-generated-file-snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["files"] == []
    assert metadata["rejected_files"][0]["reason"] == "copy_failed"
    assert metadata["rejected_files"][0]["path"] == "src/app.txt"
    assert not (snapshot_root / "src" / "app.txt").exists()
    assert outside.read_text(encoding="utf-8") == "escape"


def test_generated_file_snapshot_rejects_symlinked_source_parent(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.txt").write_text("content", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    outside = base_dir / "outside"
    outside.mkdir()
    (stage_dir / "generated-file-sources").symlink_to(
        outside,
        target_is_directory=True,
    )
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        snapshot_generated_file_workspace(
            alpha_output,
            workspace,
            changed_paths={"src/app.txt"},
        )

    assert outside.exists()
