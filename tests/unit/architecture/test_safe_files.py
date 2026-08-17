import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from crewplane.architecture import safe_files
from crewplane.architecture.safe_files import (
    ensure_contained_directory,
    replace_contained_file,
)


def test_ensure_contained_directory_allows_concurrent_creation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(ensure_contained_directory, root, "review-state")
            for _ in range(8)
        ]
        paths = [future.result() for future in futures]

    assert paths == [root / "review-state"] * 8


def test_replace_contained_file_publishes_into_the_open_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "nested").mkdir()
    source = tmp_path / "private-output.md"
    source.write_text("published", encoding="utf-8")

    published = replace_contained_file(root, "nested/output.md", source)

    assert published == root / "nested/output.md"
    assert published.read_text(encoding="utf-8") == "published"
    assert not source.exists()


def test_replace_contained_file_rejects_a_symlinked_destination_parent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "nested").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    source = tmp_path / "private-output.md"
    source.write_text("private", encoding="utf-8")

    with pytest.raises(OSError):
        replace_contained_file(root, "nested/output.md", source)

    assert not (outside / "output.md").exists()


def test_replace_contained_file_does_not_clobber_a_racing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "private-output.md"
    source.write_text("private", encoding="utf-8")
    original_link = os.link

    def precreate_destination(
        source_path: Path,
        destination_name: str,
        *,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        descriptor = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        try:
            os.write(descriptor, b"peer")
        finally:
            os.close(descriptor)
        original_link(
            source_path,
            destination_name,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(safe_files.os, "link", precreate_destination)

    with pytest.raises(FileExistsError):
        replace_contained_file(root, "output.md", source)

    assert source.read_text(encoding="utf-8") == "private"
    assert (root / "output.md").read_text(encoding="utf-8") == "peer"


def test_replace_contained_file_rejects_a_hardlinked_source(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "private-output.md"
    source.write_text("private", encoding="utf-8")
    os.link(source, root / "peer-link.md")

    with pytest.raises(ValueError, match="single-link"):
        replace_contained_file(root, "output.md", source)

    assert not (root / "output.md").exists()


def test_replace_contained_file_rolls_back_its_link_after_verification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "private-output.md"
    source.write_text("private", encoding="utf-8")

    original_unlink = Path.unlink

    def reject_source_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == source:
            raise OSError("source unlink failed")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_source_unlink)

    with pytest.raises(OSError, match="source unlink failed"):
        replace_contained_file(root, "output.md", source)

    assert source.read_text(encoding="utf-8") == "private"
    assert not (root / "output.md").exists()
