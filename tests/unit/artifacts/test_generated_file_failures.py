from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from crewplane.artifacts.generated_files.catalog import (
    build_generated_file_links_section,
    generated_file_links_for_content,
    generated_file_snapshot_rejection_summary,
    snapshot_generated_file_workspace,
)
from crewplane.artifacts.generated_files.detection import (
    GeneratedFileLink,
    GeneratedFileReferenceDetector,
)
from crewplane.artifacts.generated_files.paths import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    GENERATED_FILE_SOURCE_METADATA_NAME,
    is_reserved_workspace_path,
)
from crewplane.artifacts.generated_files.snapshot_io import (
    copy_generated_file_snapshot_candidate,
)
from crewplane.artifacts.generated_files.snapshot_policy import (
    GeneratedFileSnapshotPolicy,
    select_generated_file_snapshot_candidates,
)


@pytest.mark.parametrize(
    "name",
    [
        ".crewplane",
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        "__pycache__",
        "execution-results",
        "execution-stages",
        "node_modules",
        ".crewplane-generated-file-source.json",
        ".crewplane-generated-file-snapshot.json",
    ],
)
def test_generated_file_reservations_are_case_sensitive_root_components(name) -> None:
    assert is_reserved_workspace_path(Path(name))
    assert is_reserved_workspace_path(Path(name) / "file.txt")
    assert not is_reserved_workspace_path(Path("nested") / name)
    assert not is_reserved_workspace_path(Path(name.upper()))
    assert not is_reserved_workspace_path(Path(name + "-output"))


def test_missing_snapshot_metadata_allows_ordinary_generated_file_links(
    tmp_path,
) -> None:
    assert not is_reserved_workspace_path(Path())
    report = tmp_path / "report.md"
    report.write_text("retained")
    result = generated_file_links_for_content(
        "## Generated Files\n- `report.md`\n", tmp_path, tmp_path / "result.md", "draft"
    )
    assert result.links == (GeneratedFileLink("report.md", report),)


@pytest.mark.parametrize(
    "metadata",
    [
        "{",
        "null",
        "[]",
        '{"files":null}',
        '{"files":[null,{}, {"path":4},{"path":"../escape.md"}]}',
    ],
)
def test_invalid_snapshot_catalogs_do_not_expose_unlisted_files(
    tmp_path: Path, metadata: str
) -> None:
    (tmp_path / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_text(metadata)
    (tmp_path / "report.md").write_text("private")

    result = generated_file_links_for_content(
        "## Generated Files\n- `report.md`\n", tmp_path, tmp_path / "result.md", "draft"
    )

    assert result.links == ()
    assert result.warnings == ()


@pytest.mark.parametrize(
    "metadata", ["{", "null", "[]", '{"source_root":null}', '{"source_root":""}']
)
def test_invalid_source_metadata_still_resolves_local_snapshot_files(
    tmp_path: Path, metadata: str
) -> None:
    (tmp_path / GENERATED_FILE_SOURCE_METADATA_NAME).write_text(metadata)
    report = tmp_path / "report.md"
    report.write_text("retained")

    result = generated_file_links_for_content(
        "## Generated Files\n- `report.md`\n", tmp_path, tmp_path / "result.md", "draft"
    )

    assert result.links == (GeneratedFileLink("report.md", report),)


@pytest.mark.parametrize(
    "metadata",
    [
        "{",
        "null",
        '{"rejected_files":null}',
        '{"rejected_files":[null],"rejected_file_count":true}',
    ],
)
def test_invalid_rejection_metadata_does_not_invent_rejection_counts(
    tmp_path: Path, metadata: str
) -> None:
    (tmp_path / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_text(metadata)

    summary = generated_file_snapshot_rejection_summary(tmp_path)

    assert summary.total_count == 0
    assert summary.recorded_files == ()
    assert summary.truncated is False


@pytest.mark.parametrize(
    "metadata_name",
    [GENERATED_FILE_SOURCE_METADATA_NAME, GENERATED_FILE_SNAPSHOT_METADATA_NAME],
)
def test_unreadable_snapshot_metadata_is_handled_without_exposing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata_name: str
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    metadata = snapshot / metadata_name
    original_source = tmp_path / "original"
    original_source.mkdir()
    report = snapshot / "report.md"
    report.write_text("retained")
    metadata.write_text(
        json.dumps(
            {"source_root": str(original_source)}
            if metadata_name == GENERATED_FILE_SOURCE_METADATA_NAME
            else {
                "files": [{"path": "report.md"}],
                "rejected_file_count": 1,
                "rejected_files": [{"path": "large.md"}],
            }
        )
    )
    reference = (
        original_source / "report.md"
        if metadata_name == GENERATED_FILE_SOURCE_METADATA_NAME
        else report
    )
    content = f"Created `{reference}`."
    assert generated_file_links_for_content(
        content, snapshot, tmp_path / "result.md", "draft"
    ).links == (GeneratedFileLink("report.md", report),)
    original_read = Path.read_text

    def read(path: Path, *args: object, **kwargs: object) -> str:
        if path == metadata:
            raise OSError("metadata unavailable")
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        links = generated_file_links_for_content(
            content, snapshot, tmp_path / "result.md", "draft"
        )
        summary = generated_file_snapshot_rejection_summary(snapshot)

    assert links.links == ()
    assert summary.total_count == 0


def test_generated_links_deduplicate_labels_and_handle_empty_input(
    tmp_path: Path,
) -> None:
    result = tmp_path / "result.md"
    assert build_generated_file_links_section(result, ()) is None
    section = build_generated_file_links_section(
        result,
        (
            GeneratedFileLink("report", tmp_path / "first.md"),
            GeneratedFileLink("report", tmp_path / "second.md"),
        ),
    )
    assert section == "## Generated Files\n\n- [report](first.md)\n"


def test_snapshot_candidates_exclude_reserved_outside_missing_and_unclaimed_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    safe = workspace / "report.md"
    safe.write_text("retained")
    ignored = workspace / "ignored.md"
    ignored.write_text("unclaimed")
    reserved = workspace / ".crewplane" / "private.md"
    reserved.parent.mkdir()
    reserved.write_text("private")
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    output = tmp_path / "response.md"
    output.write_text("## Generated Files\n- `report.md`\n- `ignored.md`\n")

    snapshot = snapshot_generated_file_workspace(
        output,
        workspace,
        candidate_files=[safe, safe, reserved, outside, workspace / "missing.md"],
        explicit_claims_only=True,
    )

    metadata = json.loads(
        (snapshot / GENERATED_FILE_SNAPSHOT_METADATA_NAME).read_text()
    )
    assert [entry["path"] for entry in metadata["files"]] == ["report.md"]
    assert (snapshot / "report.md").read_text() == "retained"
    assert not (snapshot / "ignored.md").exists()
    assert not (snapshot / ".crewplane").exists()


@pytest.mark.parametrize("limit", ["per-file", "total"])
def test_snapshot_size_limits_record_the_rejected_candidate(
    tmp_path: Path, limit: str
) -> None:
    source = tmp_path / "report.md"
    source.write_bytes(b"12345")
    selection = select_generated_file_snapshot_candidates(
        [source],
        GeneratedFileSnapshotPolicy(
            resolved_workspace_root=tmp_path,
            changed_paths=None,
            explicit_labels=set(),
            baseline_supplied=True,
            file_count_limit=10,
            per_file_size_limit=4 if limit == "per-file" else 10,
            total_size_limit=4 if limit == "total" else 10,
            rejection_detail_limit=10,
        ),
    )

    assert selection.candidates == ()
    assert selection.rejections.rejected_file_count == 1
    assert selection.rejections.rejected_files[0]["reason"] == (
        "per_file_size_limit" if limit == "per-file" else "total_size_limit"
    )
    assert selection.rejections.rejected_files[0]["path"] == "report.md"
    assert source.read_bytes() == b"12345"


@pytest.mark.parametrize(
    "change",
    ["deleted-parent", "file-parent", "directory", "hardlink", "replacement", "size"],
)
def test_snapshot_copy_revalidates_selected_files_and_cleans_failed_targets(
    tmp_path: Path, change: str
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    source = nested / "report.md"
    source.write_bytes(b"original")
    selection = select_generated_file_snapshot_candidates(
        [source],
        GeneratedFileSnapshotPolicy(
            resolved_workspace_root=workspace,
            changed_paths=None,
            explicit_labels={"nested/report.md"},
            baseline_supplied=True,
            file_count_limit=10,
            per_file_size_limit=100,
            total_size_limit=100,
            rejection_detail_limit=10,
        ),
    )
    candidate = selection.candidates[0]
    if change in {"deleted-parent", "file-parent"}:
        source.unlink()
        nested.rmdir()
        if change == "file-parent":
            nested.write_bytes(b"peer")
    elif change == "directory":
        source.unlink()
        source.mkdir()
    elif change == "hardlink":
        os.link(source, tmp_path / "peer-link.md")
    elif change == "replacement":
        replacement = nested / "replacement.md"
        replacement.write_bytes(b"replaced")
        replacement.replace(source)
    else:
        source.write_bytes(b"longer than original")
    target = tmp_path / "copy.md"

    with pytest.raises(RuntimeError, match="source"):
        copy_generated_file_snapshot_candidate(candidate, target, workspace)

    assert not target.exists()
    if change == "replacement":
        assert source.read_bytes() == b"replaced"
    if change == "hardlink":
        assert (tmp_path / "peer-link.md").read_bytes() == b"original"


@pytest.mark.parametrize(
    "reference",
    [
        "#section",
        "~/report.md",
        "https://example.test/report.md",
        "<missing.md",
        "''",
        "'<#section>'",
        "'<https://example.test/report.md>'",
        "README",
        ".git/report.md",
        ".crewplane-generated-file-source.json",
        "missing.md",
        "../outside.md",
        "<~/report.md>",
    ],
)
def test_generated_file_detector_rejects_non_file_and_unsafe_references(
    tmp_path: Path, reference: str
) -> None:
    (tmp_path / "report.md").write_text("retained")
    (tmp_path / "README").write_text("extensionless")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "report.md").write_text("internal")
    (tmp_path / GENERATED_FILE_SOURCE_METADATA_NAME).write_text("{}")
    detector = GeneratedFileReferenceDetector(tmp_path)

    assert detector.detect(f"## Generated Files\n- `{reference}`\n") == ()


def test_generated_file_detector_rejects_paths_outside_both_source_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_source = tmp_path / "original"
    original_source.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    detector = GeneratedFileReferenceDetector(workspace, original_source)

    assert detector.detect(f"Created `{outside}`.") == ()


def test_generated_file_detector_handles_workspace_disappearing_after_creation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    detector = GeneratedFileReferenceDetector(workspace)
    workspace.rmdir()

    assert detector.detect("Created `report.md`.") == ()


def test_generated_file_claims_associate_each_path_with_its_nearest_action(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "created.md"
    generated.write_text("generated")
    unchanged = tmp_path / "unchanged.md"
    unchanged.write_text("unchanged")
    detector = GeneratedFileReferenceDetector(tmp_path)

    assert detector.detect("Created `created.md`; never updated `unchanged.md`.") == (
        generated,
    )
    assert detector.detect("`created.md` was written.") == (generated,)


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_snapshot_publication_rejects_an_unsafe_destination_root(
    tmp_path: Path, kind: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "output.md"
    output.write_text("No generated files.")
    snapshot = tmp_path / "snapshots" / "output"
    snapshot.parent.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    if kind == "file":
        snapshot.write_text("peer")
    else:
        snapshot.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="not a directory"):
        snapshot_generated_file_workspace(output, workspace, snapshot_root=snapshot)

    assert list(external.iterdir()) == []
    if kind == "file":
        assert snapshot.read_text() == "peer"
