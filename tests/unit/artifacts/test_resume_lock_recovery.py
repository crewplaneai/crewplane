from __future__ import annotations

import json
import os

import pytest

import crewplane.artifacts.locks.process_identity as process_identity
from crewplane.artifacts.locks import (
    LOCK_OWNER_FILENAME,
    ResumeLockError,
    acquire_same_context_lock,
)
from crewplane.artifacts.locks.process_identity import ProcessInspector
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    make_run_manifest,
    sha256_hex,
    write_run_manifest,
)
from tests.helpers.resume_locks import (
    FakeProcessInspector,
    write_manifest_at_run_key,
)


def test_stale_lock_finalizes_running_manifest_as_cancelled(tmp_path) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(200, "new", live=False),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "cancelled"
        assert manifest["cancel_reason"] == "stale_lock_recovered"
    finally:
        lock.release()


def test_pid_start_identity_mismatch_recovers_stale_lock(
    tmp_path,
    monkeypatch,
) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )

    def process_start(pid: int) -> str:
        if pid == 100:
            return "new-start"
        return "current-start"

    def pid_exists(pid: int, signal_number: int) -> None:
        del pid, signal_number

    monkeypatch.setattr(process_identity, "process_start_identity", process_start)
    monkeypatch.setattr(process_identity.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(process_identity.os, "kill", pid_exists)

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=ProcessInspector(),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "cancelled"
        assert manifest["cancel_reason"] == "stale_lock_recovered"
        assert lock.owner_token != stale.owner_token
    finally:
        lock.release()


@pytest.mark.parametrize(
    (
        "manifest_run_id",
        "manifest_run_key_name",
        "workflow_identity",
        "workflow_signature",
    ),
    [
        ("other-run", "workflow--source", WORKFLOW_IDENTITY, WORKFLOW_SIGNATURE),
        ("source", "workflow--other", WORKFLOW_IDENTITY, WORKFLOW_SIGNATURE),
        ("source", "workflow--source", "other/workflow.task.md", WORKFLOW_SIGNATURE),
        ("source", "workflow--source", WORKFLOW_IDENTITY, sha256_hex("other")),
    ],
)
def test_stale_lock_does_not_cancel_mismatched_manifest(
    tmp_path,
    manifest_run_id: str,
    manifest_run_key_name: str,
    workflow_identity: str,
    workflow_signature: str,
) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    manifest_path = write_manifest_at_run_key(
        tmp_path,
        "workflow--source",
        make_run_manifest(
            manifest_run_id,
            manifest_run_key_name,
            status="running",
            workflow_identity=workflow_identity,
            workflow_signature=workflow_signature,
        ),
    )

    with pytest.raises(ResumeLockError, match="does not match run manifest"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(200, "new", live=False),
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    assert stale.lock_dir.exists()
    stale.release()


def test_stale_lock_with_malformed_manifest_fails_closed(tmp_path) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    manifest_path = (
        tmp_path / "execution-stages" / "workflow--source" / "manifests" / "run.json"
    )
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ResumeLockError, match="malformed or unreadable"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(200, "new", live=False),
        )

    assert stale.lock_dir.exists()
    stale.release()


def test_stale_lock_with_unexpected_files_fails_closed_and_preserves_lock(
    tmp_path,
) -> None:
    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    (lock.lock_dir / "unexpected.txt").write_text("unsafe", encoding="utf-8")

    with pytest.raises(ResumeLockError, match="unexpected files"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(200, "new", live=False),
        )

    assert lock.lock_dir.exists()
    lock.release()


def test_stale_lock_with_unsafe_run_key_fails_closed_and_preserves_lock(
    tmp_path,
) -> None:
    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    owner_path = lock.lock_dir / LOCK_OWNER_FILENAME
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["run_id"] = "source"
    owner["run_key_name"] = "../../outside"
    owner_path.write_text(json.dumps(owner), encoding="utf-8")

    with pytest.raises(ResumeLockError, match="unexpected files"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            grace_seconds=0,
            process_inspector=FakeProcessInspector(200, "new"),
        )

    assert lock.lock_dir.exists()
    lock.release()


def test_stale_lock_with_symlinked_manifest_directory_fails_closed(
    tmp_path,
) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink support is unavailable")
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    outside = tmp_path / "outside-manifests"
    outside.mkdir()
    run_dir = tmp_path / "execution-stages" / "workflow--source"
    run_dir.mkdir(parents=True)
    try:
        (run_dir / "manifests").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ResumeLockError, match="symlink"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(200, "new", live=False),
        )

    assert stale.lock_dir.exists()
    stale.release()


def test_stale_lock_with_symlinked_run_manifest_fails_closed(
    tmp_path,
) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink support is unavailable")
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    outside_manifest = write_run_manifest(
        tmp_path / "outside-container",
        make_run_manifest("source", "workflow--source", status="running"),
    )
    manifest_dir = tmp_path / "execution-stages" / "workflow--source" / "manifests"
    manifest_dir.mkdir(parents=True)
    try:
        (manifest_dir / "run.json").symlink_to(outside_manifest)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ResumeLockError, match="symlink"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(200, "new", live=False),
        )

    assert stale.lock_dir.exists()
    stale.release()


def test_stale_lock_with_hardlinked_run_manifest_fails_closed(
    tmp_path,
) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    source_manifest = write_run_manifest(
        tmp_path / "source-container",
        make_run_manifest("source", "workflow--source", status="running"),
    )
    manifest_path = (
        tmp_path / "execution-stages" / "workflow--source" / "manifests" / "run.json"
    )
    manifest_path.parent.mkdir(parents=True)
    try:
        os.link(source_manifest, manifest_path)
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    with pytest.raises(ResumeLockError, match="safe file"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(200, "new", live=False),
        )

    assert stale.lock_dir.exists()
    stale.release()


def test_recovered_owner_is_rechecked_after_quarantine(tmp_path) -> None:
    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )

    with pytest.raises(ResumeLockError, match="live same-context"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(
                200,
                "new",
                live_checks=[False, True],
            ),
        )

    assert lock.lock_dir.exists()
    lock.release()
