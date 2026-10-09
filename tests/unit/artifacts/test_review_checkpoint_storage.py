from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from crewplane.artifacts import atomic
from crewplane.artifacts.naming import review_checkpoint_relative_path
from crewplane.artifacts.resume.checkpoint_store import (
    publish_review_checkpoint,
    read_review_checkpoint,
)
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from tests.helpers.platforms import symlink_or_skip
from tests.helpers.review_checkpoints import checkpoint_payload


def stored_checkpoint(root: Path) -> OpenReviewCheckpoint:
    payload = checkpoint_payload()
    content = b"candidate"
    payload["files"][0]["signature"] = (
        len(content),
        hashlib.sha256(content).hexdigest(),
    )
    record = OpenReviewCheckpoint.model_validate(payload)
    path = root / record.files[0].relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    publish_review_checkpoint(root, record)
    return record


def test_checkpoint_names_are_collision_safe_and_bounded() -> None:
    names = {
        review_checkpoint_relative_path(node)
        for node in ("a b", "a-b", "a.b", "a/b", "a" * 1000)
    }
    assert len(names) == 5
    assert all(
        len(path.name) <= 255
        and path.parent.as_posix() == "manifests/review-checkpoints"
        for path in names
    )


@pytest.mark.parametrize(
    "damage", ["missing", "changed", "symlink", "hardlink", "directory"]
)
def test_unsafe_or_changed_dependencies_do_not_replace_marker(
    tmp_path: Path, damage: str
) -> None:
    record = stored_checkpoint(tmp_path)
    marker = tmp_path / review_checkpoint_relative_path("a")
    before = marker.read_bytes()
    dependency = tmp_path / record.files[0].relative_path
    if damage == "changed":
        dependency.write_text("different", encoding="utf-8", newline="\n")
    elif damage == "hardlink":
        os.link(dependency, tmp_path / "alias")
    else:
        dependency.unlink()
        if damage == "symlink":
            target = tmp_path / "outside"
            target.write_text("candidate", encoding="utf-8", newline="\n")
            symlink_or_skip(dependency, target)
        elif damage == "directory":
            dependency.mkdir()
    with pytest.raises(ValueError):
        publish_review_checkpoint(tmp_path, record)
    assert marker.read_bytes() == before


@pytest.mark.parametrize("after_replace", [False, True])
def test_publication_failure_validates_surviving_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_replace: bool
) -> None:
    old = stored_checkpoint(tmp_path)
    new = old.model_copy(update={"project_observation": None})
    if after_replace:
        from crewplane.architecture import safe_files_windows

        owner, name = (
            (safe_files_windows, "rename_contained_file")
            if os.name == "nt"
            else (Path, "replace")
        )
        publish = getattr(owner, name)

        def fail_after_publication(*args, **kwargs):
            publish(*args, **kwargs)
            raise OSError("disk failure after publication")

        monkeypatch.setattr(owner, name, fail_after_publication)
    else:

        def fail_sync(descriptor):
            assert descriptor >= 0
            raise OSError("disk failure before publication")

        monkeypatch.setattr(atomic.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="disk failure"):
        publish_review_checkpoint(tmp_path, new)
    assert read_review_checkpoint(tmp_path, "a") == (new if after_replace else old)


def test_orphan_outputs_do_not_authorize_checkpoint_reuse(tmp_path: Path) -> None:
    record = stored_checkpoint(tmp_path)
    (tmp_path / "a" / "orphan.md").write_text("uncommitted review")
    assert read_review_checkpoint(tmp_path, "a") == record
    (tmp_path / review_checkpoint_relative_path("a")).unlink()
    assert read_review_checkpoint(tmp_path, "a") is None
