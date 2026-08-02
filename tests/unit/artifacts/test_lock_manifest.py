import os
from pathlib import Path

import pytest

from crewplane.artifacts.locks.manifest import (
    LockManifestError,
    LockRunMetadata,
    ensure_no_symlink_manifest_components,
    ensure_owner_path_contained,
    finalize_stale_running_run,
    has_symlink_component,
    owner_manifest_path,
    path_is_symlink,
    read_owner_manifest,
    safe_owner_manifest_path,
)
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_SIGNATURE,
    make_run_manifest,
    write_run_manifest,
)


def metadata(
    run_id: str | None = "source",
    run_key_name: str | None = "workflow--source",
) -> LockRunMetadata:
    return LockRunMetadata(
        run_id=run_id,
        run_key_name=run_key_name,
        workflow_identity=WORKFLOW_IDENTITY,
        workflow_signature=WORKFLOW_SIGNATURE,
    )


def test_finalize_ignores_absent_run_metadata_or_manifest(tmp_path: Path) -> None:
    finalize_stale_running_run(tmp_path, metadata(None, None))
    finalize_stale_running_run(tmp_path, metadata())


@pytest.mark.parametrize(
    ("run_id", "run_key_name"),
    [
        pytest.param(None, "workflow--source", id="missing-id"),
        pytest.param("source", None, id="missing-key"),
    ],
)
def test_finalize_rejects_incomplete_run_metadata(
    tmp_path: Path,
    run_id: str | None,
    run_key_name: str | None,
) -> None:
    with pytest.raises(LockManifestError, match="metadata is incomplete"):
        finalize_stale_running_run(tmp_path, metadata(run_id, run_key_name))


def test_finalize_leaves_terminal_manifest_unchanged(tmp_path: Path) -> None:
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="succeeded"),
    )
    before = manifest_path.read_text(encoding="utf-8")

    finalize_stale_running_run(tmp_path, metadata())

    assert manifest_path.read_text(encoding="utf-8") == before


def test_read_owner_manifest_propagates_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "run.json"
    original_read_text = Path.read_text

    def blocked_read(self: Path, *args: object, **kwargs: object) -> str:
        if self == manifest_path:
            raise PermissionError("blocked")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocked_read)

    with pytest.raises(PermissionError, match="blocked"):
        read_owner_manifest(manifest_path)


def test_read_owner_manifest_wraps_unreadable_content(tmp_path: Path) -> None:
    manifest_path = tmp_path / "run.json"
    manifest_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(LockManifestError, match="malformed or unreadable"):
        read_owner_manifest(manifest_path)


def test_owner_manifest_path_validates_run_key(tmp_path: Path) -> None:
    assert owner_manifest_path(tmp_path, "workflow--source") == (
        tmp_path / "execution-stages" / "workflow--source" / "manifests" / "run.json"
    )
    with pytest.raises(LockManifestError, match="not safely contained"):
        owner_manifest_path(tmp_path, "../outside")


def test_safe_owner_manifest_path_handles_missing_and_non_directory_parent(
    tmp_path: Path,
) -> None:
    assert safe_owner_manifest_path(tmp_path, "workflow--source") is None
    run_dir = tmp_path / "execution-stages" / "workflow--source"
    run_dir.mkdir(parents=True)
    (run_dir / "manifests").write_text("not a directory", encoding="utf-8")

    assert safe_owner_manifest_path(tmp_path, "workflow--source") is None


def test_safe_owner_manifest_path_rejects_directory_and_hard_link(
    tmp_path: Path,
) -> None:
    manifest_path = (
        tmp_path / "execution-stages" / "workflow--source" / "manifests" / "run.json"
    )
    manifest_path.mkdir(parents=True)
    with pytest.raises(LockManifestError, match="not a safe file"):
        safe_owner_manifest_path(tmp_path, "workflow--source")

    manifest_path.rmdir()
    manifest_path.write_text("{}", encoding="utf-8")
    os.link(manifest_path, tmp_path / "second-link")
    with pytest.raises(LockManifestError, match="not a safe file"):
        safe_owner_manifest_path(tmp_path, "workflow--source")


def test_containment_helpers_reject_escape_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(LockManifestError, match="not safely contained"):
        ensure_owner_path_contained(root, outside / "run.json")
    with pytest.raises(LockManifestError, match="not safely contained"):
        ensure_no_symlink_manifest_components(root, outside / "run.json")

    linked = root / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(LockManifestError, match="contains a symlink"):
        ensure_no_symlink_manifest_components(root, linked / "run.json")
    assert has_symlink_component(linked / "run.json")
    assert path_is_symlink(linked)


def test_path_is_symlink_returns_false_for_missing_path(tmp_path: Path) -> None:
    assert not path_is_symlink(tmp_path / "missing")
