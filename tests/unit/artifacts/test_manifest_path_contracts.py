from pathlib import Path

import pytest

from crewplane.artifacts.locks.manifest import (
    LockManifestError,
    ensure_no_symlink_manifest_components,
    ensure_owner_path_contained,
    owner_manifest_path,
    safe_owner_manifest_path,
)
from crewplane.artifacts.run_history import (
    RunHistoryError,
    find_same_context_runs,
)
from tests.helpers.platforms import symlink_or_skip
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    make_run_manifest,
    write_run_manifest,
)


@pytest.mark.parametrize("consumer", ["lock", "history"])
@pytest.mark.parametrize("location", ["root", "candidate"])
@pytest.mark.parametrize("failure_type", [OSError, PermissionError])
def test_containment_resolution_order_and_exception_causes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    location: str,
    failure_type: type[OSError],
) -> None:
    manifest_path = write_run_manifest(
        tmp_path, make_run_manifest("source", "workflow--source")
    )
    original = manifest_path.read_bytes()
    root = tmp_path / "execution-stages"
    candidate = manifest_path.parents[1]
    target = root if location == "root" else candidate
    failure = failure_type("resolution failed")
    calls = []
    error_type = LockManifestError if consumer == "lock" else RunHistoryError
    message = (
        "Cannot inspect stale run manifest safely."
        if consumer == "lock"
        else "Cannot inspect run history path safely."
    )

    def resolve(path: Path, strict: bool = False) -> Path:
        assert strict is False
        calls.append(path)
        if path == target:
            raise failure
        return path

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", resolve)
        with pytest.raises(
            PermissionError if failure_type is PermissionError else error_type
        ) as caught:
            if consumer == "lock":
                ensure_owner_path_contained(root, candidate)
            else:
                find_same_context_runs(
                    tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
                )
    assert calls == ([root] if location == "root" else [root, candidate])
    if failure_type is PermissionError:
        assert caught.value is failure
    else:
        assert str(caught.value) == message
        assert caught.value.__cause__ is failure
    assert manifest_path.read_bytes() == original


def test_manifest_path_escape_and_missing_descendant_contract(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside" / "run.json"
    with pytest.raises(LockManifestError) as caught:
        ensure_owner_path_contained(root, outside)
    assert str(caught.value) == "Lock owner run metadata is not safely contained."
    assert caught.value.__cause__ is None
    with pytest.raises(LockManifestError) as caught:
        ensure_no_symlink_manifest_components(root, outside)
    assert str(caught.value) == "Lock owner run metadata is not safely contained."
    assert type(caught.value.__cause__) is ValueError
    assert str(caught.value.__cause__) == str(_relative_path_error(root, outside))
    missing = root / "missing" / "run.json"
    assert ensure_owner_path_contained(root, missing) is None
    assert ensure_no_symlink_manifest_components(root, missing) is None

    linked = tmp_path / "linked"
    symlink_or_skip(linked, root, target_is_directory=True)
    symlink_or_skip(root / "linked", tmp_path / "absent")
    for inspected_root, candidate in [
        (linked, linked / "missing"),
        (linked / "missing", linked / "missing" / "run.json"),
        (root, root / "linked" / "missing"),
        (linked, outside),
    ]:
        with pytest.raises(LockManifestError) as caught:
            ensure_no_symlink_manifest_components(inspected_root, candidate)
        assert str(caught.value) == "Stale run manifest path contains a symlink."
        assert caught.value.__cause__ is None


def _relative_path_error(root, candidate):
    with pytest.raises(ValueError) as caught:
        candidate.relative_to(root)
    return caught.value


def test_manifest_consumers_keep_missing_file_inspection_order(tmp_path: Path) -> None:
    root = tmp_path / "execution-stages"
    real_run_dir = root / "real"
    real_run_dir.mkdir(parents=True)
    run_dir = root / "workflow--source"
    symlink_or_skip(run_dir, real_run_dir, target_is_directory=True)

    assert (
        find_same_context_runs(
            tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
        )
        == ()
    )
    with pytest.raises(LockManifestError) as caught:
        safe_owner_manifest_path(tmp_path, run_dir.name)
    assert str(caught.value) == "Stale run manifest path contains a symlink."
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("consumer", ["lock", "history"])
@pytest.mark.parametrize("location", ["root", "candidate"])
@pytest.mark.parametrize("failure_type", [PermissionError, ValueError])
def test_symlink_inspection_propagates_filesystem_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    location: str,
    failure_type: type[Exception],
) -> None:
    manifest_path = write_run_manifest(
        tmp_path, make_run_manifest("source", "workflow--source")
    )
    root = tmp_path / "execution-stages"
    target = root if location == "root" else manifest_path.parents[1]
    original_lstat = Path.lstat
    failure = failure_type("cannot inspect path")

    def lstat(path):
        if path == target:
            raise failure
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(failure_type) as caught:
        if consumer == "lock":
            ensure_no_symlink_manifest_components(root, manifest_path)
        else:
            find_same_context_runs(
                tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
            )
    assert caught.value is failure


@pytest.mark.parametrize("hardlink", [False, True])
def test_manifest_inspection_preserves_metadata_bytes(tmp_path, hardlink):
    run_manifest = make_run_manifest("source", "workflow--source")
    manifest = write_run_manifest(tmp_path, run_manifest)
    original = manifest.read_bytes()
    if hardlink:
        (tmp_path / "copy").hardlink_to(manifest)
        for inspect, args, error_type, message in [
            (
                safe_owner_manifest_path,
                (tmp_path, run_manifest.run_key_name),
                LockManifestError,
                "Stale run manifest is not a safe file.",
            ),
            (
                find_same_context_runs,
                (tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE),
                RunHistoryError,
                "Run history metadata path is not a safe file.",
            ),
        ]:
            with pytest.raises(error_type) as caught:
                inspect(*args)
            assert str(caught.value) == message
            assert caught.value.__cause__ is None
    else:
        assert safe_owner_manifest_path(tmp_path, run_manifest.run_key_name) == manifest
        records = find_same_context_runs(
            tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
        )
        assert [record.manifest_path for record in records] == [manifest]
        assert records[0].manifest == run_manifest
    assert manifest.read_bytes() == original


@pytest.mark.parametrize("failure_type", [OSError, PermissionError])
def test_owner_manifest_path_resolution_failure_policy(
    tmp_path, monkeypatch, failure_type
):
    failure = failure_type("resolution failed")

    def resolve(path, strict=False):
        assert path == tmp_path / "execution-stages"
        assert strict is False
        raise failure

    monkeypatch.setattr(Path, "resolve", resolve)
    if failure_type is PermissionError:
        with pytest.raises(PermissionError) as caught:
            owner_manifest_path(tmp_path, "workflow--source")
        assert caught.value is failure
    else:
        assert owner_manifest_path(tmp_path, "workflow--source") is None
