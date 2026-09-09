from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from unittest.mock import mock_open

import pytest

from crewplane.artifacts.locks import (
    LOCK_OWNER_FILENAME,
    ResumeLockError,
    acquire_same_context_lock,
    run_lock_activity,
)
from crewplane.artifacts.locks.manifest import TerminalRecoveryIntent
from crewplane.artifacts.locks.process_identity import (
    ProcessIdentity,
    ProcessInspector,
    process_start_identity,
)
from tests.helpers.resume_locks import FakeProcessInspector


@pytest.mark.parametrize("live", [False, True])
def test_run_activity_distinguishes_matching_locks_from_other_runs(
    tmp_path: Path, request: pytest.FixtureRequest, live: bool
) -> None:
    inspector = FakeProcessInspector(123, "start", live=live)
    lock = acquire_same_context_lock(
        tmp_path, "flow", "identity", "signature", process_inspector=inspector
    )
    request.addfinalizer(lock.release)
    lock.update_run("run-a", "flow-run-a")
    (tmp_path / "locks" / "unrelated-file").write_bytes(b"ignored")
    (tmp_path / "locks" / "symlink").symlink_to(lock.lock_dir, target_is_directory=True)

    assert run_lock_activity(tmp_path, "flow-run-a", inspector) == (
        "live" if live else "stale"
    )
    assert run_lock_activity(tmp_path, "flow-run-b", inspector) == "none"


@pytest.mark.parametrize("kind", ["file", "symlink", "ownerless"])
def test_unverifiable_lock_storage_is_never_reported_inactive(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "locks"
    if kind == "file":
        root.write_bytes(b"invalid")
    elif kind == "symlink":
        target = tmp_path / "elsewhere"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    else:
        (root / "unowned").mkdir(parents=True)

    assert run_lock_activity(tmp_path, "flow-run-a") == "unverifiable"


@pytest.mark.parametrize("operation", ["update", "recovery"])
@pytest.mark.parametrize("owner_change", ["missing", "different-token"])
def test_lost_ownership_prevents_lock_updates(
    tmp_path: Path, request: pytest.FixtureRequest, operation: str, owner_change: str
) -> None:
    lock = acquire_same_context_lock(
        tmp_path,
        "flow",
        "identity",
        "signature",
        process_inspector=FakeProcessInspector(123, "start"),
    )
    request.addfinalizer(lock.release)
    lock.update_run("run-a", "flow-run-a")
    owner_path = lock.lock_dir / LOCK_OWNER_FILENAME
    if owner_change == "missing":
        owner_path.unlink()
        before = None
    else:
        payload = json.loads(owner_path.read_text())
        payload["owner_token"] = "peer-owner"
        before = json.dumps(payload)
        owner_path.write_text(before)

    if operation == "update":
        with pytest.raises(ResumeLockError, match="owned by another process"):
            lock.update_run("run-b", "flow-run-b")
    else:
        with pytest.raises(ResumeLockError, match="owned by another process"):
            lock.record_terminal_recovery("outcome_selected", "failed", "failed")

    assert (owner_path.read_text() if owner_path.exists() else None) == before


@pytest.mark.parametrize(
    ("violation", "message"),
    [
        ("no-run", "before run ownership"),
        ("skip-first", "must begin with outcome_selected"),
        ("change-outcome", "outcome cannot change"),
        ("skip-phase", "phase cannot skip or regress"),
        ("regress", "phase cannot skip or regress"),
    ],
)
def test_terminal_recovery_rejects_invalid_transitions(
    tmp_path: Path, request: pytest.FixtureRequest, violation: str, message: str
) -> None:
    lock = acquire_same_context_lock(
        tmp_path,
        "flow",
        "identity",
        "signature",
        process_inspector=FakeProcessInspector(123, "start"),
    )
    request.addfinalizer(lock.release)
    if violation != "no-run":
        lock.update_run("run-a", "flow-run-a")
    if violation in {"change-outcome", "skip-phase", "regress"}:
        lock.record_terminal_recovery("outcome_selected", "failed", "failed")
    if violation == "regress":
        lock.record_terminal_recovery("terminal_views_published", "failed", "failed")
    owner_path = lock.lock_dir / LOCK_OWNER_FILENAME
    before = owner_path.read_bytes()
    phase = {
        "no-run": "outcome_selected",
        "skip-first": "terminal_views_published",
        "change-outcome": "terminal_views_published",
        "skip-phase": "observer_shutdown_complete",
        "regress": "outcome_selected",
    }[violation]

    with pytest.raises(ResumeLockError, match=message):
        lock.record_terminal_recovery(
            phase, "failed", "different" if violation == "change-outcome" else "failed"
        )

    assert owner_path.read_bytes() == before


def test_terminal_recovery_repeated_phase_is_idempotent(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    lock = acquire_same_context_lock(
        tmp_path,
        "flow",
        "identity",
        "signature",
        process_inspector=FakeProcessInspector(123, "start"),
    )
    request.addfinalizer(lock.release)
    lock.update_run("run-a", "flow-run-a")
    lock.record_terminal_recovery("outcome_selected", "succeeded", None)
    path = lock.lock_dir / LOCK_OWNER_FILENAME
    before = path.stat()
    payload = path.read_bytes()

    lock.record_terminal_recovery("outcome_selected", "succeeded", None)

    assert path.read_bytes() == payload
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize(
    "field", ["owner_token", "workflow_identity", "workflow_signature"]
)
def test_takeover_rechecks_owner_after_rename_and_restores_changed_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    field: str,
) -> None:
    inspector = FakeProcessInspector(123, "start")
    lock = acquire_same_context_lock(
        tmp_path, "flow", "identity", "signature", process_inspector=inspector
    )
    request.addfinalizer(lock.release)
    original_replace = Path.replace

    def rename(path: Path, target: Path) -> Path:
        result = original_replace(path, target)
        if path == lock.lock_dir:
            owner = target / LOCK_OWNER_FILENAME
            payload = json.loads(owner.read_text())
            payload[field] = "changed-during-takeover"
            owner.write_text(json.dumps(payload))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", rename)
        with pytest.raises(ResumeLockError, match="ownership changed|does not match"):
            acquire_same_context_lock(
                tmp_path, "flow", "identity", "signature", process_inspector=inspector
            )

    assert (
        json.loads((lock.lock_dir / LOCK_OWNER_FILENAME).read_text())[field]
        == "changed-during-takeover"
    )
    assert list((tmp_path / "locks").iterdir()) == [lock.lock_dir]


@pytest.mark.parametrize(
    ("status", "reason", "message"),
    [
        ("failed", None, "require a reason"),
        ("cancelled", None, "require a reason"),
        ("succeeded", "unexpected", "cannot include a reason"),
        ("failed", " ", "cannot be blank"),
    ],
)
def test_terminal_recovery_requires_an_outcome_consistent_reason(
    status: str, reason: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        TerminalRecoveryIntent(phase="outcome_selected", status=status, reason=reason)


@pytest.mark.parametrize(
    "stat_content", ["no closing delimiter", "123 (process) S 1 2"]
)
def test_process_start_identity_rejects_incomplete_proc_records(
    monkeypatch: pytest.MonkeyPatch, stat_content: str
) -> None:
    reader = mock_open(read_data=stat_content)
    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", reader)
        identity = process_start_identity(123)

    assert identity is None
    reader.assert_called_once_with("/proc/123/stat", encoding="utf-8")


def test_process_start_identity_handles_unreadable_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = mock_open()
    reader.side_effect = PermissionError("cannot read proc")
    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", reader)
        identity = process_start_identity(123)

    assert identity is None
    reader.assert_called_once_with("/proc/123/stat", encoding="utf-8")


@pytest.mark.parametrize("kind", ["remote-host", "missing-start", "invalid-pid"])
def test_process_liveness_never_trusts_unverifiable_identities(kind: str) -> None:
    inspector = ProcessInspector()
    identity = ProcessIdentity(
        pid=0 if kind == "invalid-pid" else os.getpid(),
        hostname=socket.gethostname() + ".other"
        if kind == "remote-host"
        else socket.gethostname(),
        start_identity=None,
    )
    if kind == "invalid-pid":
        assert inspector.is_live(identity) is False
    else:
        with pytest.raises(RuntimeError, match="different host|start identity"):
            inspector.is_live(identity)


@pytest.mark.parametrize("kind", ["invalid", "unsupported", "permission"])
def test_process_group_liveness_preserves_permission_and_platform_semantics(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    inspector = ProcessInspector()
    if kind == "unsupported":
        monkeypatch.delattr(os, "killpg")
    elif kind == "permission":

        def deny(group: int, signal: int) -> None:
            assert (group, signal) == (123, 0)
            raise PermissionError("peer group")

        monkeypatch.setattr(os, "killpg", deny)

    if kind == "permission":
        assert inspector.is_process_group_live(123) is True
    else:
        with pytest.raises(RuntimeError, match="invalid|platform"):
            inspector.is_process_group_live(0 if kind == "invalid" else 123)
