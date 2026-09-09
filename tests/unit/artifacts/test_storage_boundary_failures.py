from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from crewplane.artifacts.atomic import atomic_write_bytes
from crewplane.artifacts.directory_manager import DirectoryManager
from crewplane.artifacts.manager import OutputManager
from crewplane.artifacts.run_history import RunHistoryError, find_same_context_runs
from tests.helpers.resume import WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
from tests.helpers.resume_validation import source_record


@pytest.mark.parametrize("kind", ["result", "findings"])
@pytest.mark.parametrize("name", [".", "..", "logs", "manifests"])
def test_stage_artifact_names_cannot_alias_run_metadata(
    tmp_path: Path, kind: str, name: str
) -> None:
    directories = DirectoryManager("flow", tmp_path, False)
    resolve = (
        directories.get_stage_result_file
        if kind == "result"
        else directories.get_stage_findings_file
    )

    with pytest.raises(ValueError, match="cannot be|reserved"):
        resolve(name)

    assert not directories.results_dir.exists()


def test_stage_artifact_locators_normalize_user_stage_names(tmp_path: Path) -> None:
    directories = DirectoryManager("flow", tmp_path, False)

    assert (
        directories.get_stage_result_file("Draft Report").parent
        == directories.results_dir
    )
    assert directories.get_stage_result_file("Draft Report").name.endswith("-result.md")
    assert directories.get_stage_findings_file("Draft Report").name.endswith(
        "-findings.md"
    )


def test_run_allocation_removes_only_its_stage_directory_when_results_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_mkdir = Path.mkdir
    created: list[Path] = []
    peer_results: list[Path] = []

    def create(path: Path, *args: object, **kwargs: object) -> None:
        original_mkdir(path, *args, **kwargs)
        if path.parent.name == "execution-stages":
            created.append(path)
            result = tmp_path / "execution-results" / path.name
            original_mkdir(result, parents=True)
            (result / "peer.md").write_text("peer")
            peer_results.append(result)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", create)
        with pytest.raises(
            RuntimeError, match="Unable to allocate unique run directories"
        ):
            DirectoryManager("flow", tmp_path, False)

    assert len(created) == 2
    assert all(not path.exists() for path in created)
    assert [path.joinpath("peer.md").read_text() for path in peer_results] == [
        "peer",
        "peer",
    ]


@pytest.mark.parametrize(
    "relative_path",
    ["", "../escape.md", "/absolute.md", "nested//file.md", "./file.md"],
)
def test_preflight_artifact_writes_reject_unsafe_relative_paths(
    tmp_path: Path, relative_path: str
) -> None:
    output = OutputManager("flow", base_dir=tmp_path)

    with pytest.raises(ValueError, match="Invalid preflight artifact path"):
        output.write_preflight_text(relative_path, "private")

    assert not (output.stages_dir / "preflight").exists()
    assert not (tmp_path / "escape.md").exists()


def test_preflight_artifact_write_does_not_follow_existing_symlink(
    tmp_path: Path,
) -> None:
    output = OutputManager("flow", base_dir=tmp_path)
    peer = tmp_path / "peer.md"
    peer.write_text("retained")
    preflight = output.stages_dir / "preflight"
    preflight.mkdir()
    (preflight / "file.md").symlink_to(peer)

    with pytest.raises(ValueError, match="must not be a symlink"):
        output.write_preflight_text("file.md", "overwrite")

    assert peer.read_text() == "retained"


@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.EIO])
def test_atomic_publication_distinguishes_unsupported_directory_sync_from_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    target = tmp_path / "result.md"
    original_open = os.open

    def open_file(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == tmp_path:
            raise OSError(error_number, "directory sync unavailable")
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_file)
        if error_number == errno.EINVAL:
            assert atomic_write_bytes(target, b"published") == target
        else:
            with pytest.raises(OSError, match="directory sync unavailable") as caught:
                atomic_write_bytes(target, b"published")
            assert "sync parent directory" in caught.value.__notes__[0]

    assert target.read_bytes() == b"published"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("location", ["root", "listing", "manifest", "resolution"])
@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_history_inspection_failures_are_reported_without_changing_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    error_type: type[OSError],
) -> None:
    source = source_record(tmp_path)
    root = tmp_path / "execution-stages"
    target = source.manifest_path if location == "manifest" else root
    method = {
        "root": "lstat",
        "listing": "iterdir",
        "manifest": "lstat",
        "resolution": "resolve",
    }[location]
    original = getattr(Path, method)
    before = source.manifest_path.read_bytes()

    def inspect(path: Path, *args: object, **kwargs: object) -> object:
        if path == target:
            raise error_type("history unavailable")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, inspect)
        with pytest.raises(
            PermissionError if error_type is PermissionError else RunHistoryError
        ):
            find_same_context_runs(
                tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
            )

    assert source.manifest_path.read_bytes() == before
