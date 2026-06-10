from __future__ import annotations

import json
import shutil

import pytest

import orchestrator_cli.artifacts.locks as resume_locks
import orchestrator_cli.artifacts.process_identity as process_identity
from orchestrator_cli.artifacts.locks import (
    LOCK_OWNER_FILENAME,
    ResumeLockError,
    acquire_same_context_lock,
)
from orchestrator_cli.artifacts.naming import build_lock_name
from orchestrator_cli.artifacts.process_identity import (
    ProcessIdentity,
    ProcessInspector,
)
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
)
from tests.helpers.resume_locks import FakeProcessInspector, UnsafeProcessInspector


def test_process_inspector_treats_pid_start_identity_mismatch_as_not_live(
    monkeypatch,
) -> None:
    def kill(pid: int, signal_number: int) -> None:
        assert pid == 100
        assert signal_number == 0

    def process_start(pid: int) -> str:
        assert pid == 100
        return "new-start"

    monkeypatch.setattr(process_identity.os, "kill", kill)
    monkeypatch.setattr(process_identity, "process_start_identity", process_start)
    monkeypatch.setattr(process_identity.socket, "gethostname", lambda: "host")

    assert not ProcessInspector().is_live(
        ProcessIdentity(
            pid=100,
            hostname="host",
            start_identity="old-start",
        )
    )


def test_acquire_update_and_release_same_context_lock(tmp_path) -> None:
    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "start"),
    )

    lock.update_run("run", "workflow--run")
    owner = json.loads(
        (lock.lock_dir / LOCK_OWNER_FILENAME).read_text(encoding="utf-8")
    )
    assert owner["run_id"] == "run"
    assert owner["workflow_identity"] == WORKFLOW_IDENTITY
    assert WORKFLOW_IDENTITY not in lock.lock_dir.name

    lock.release()

    assert not lock.lock_dir.exists()


def test_live_owner_fails_closed(tmp_path) -> None:
    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "start"),
    )
    try:
        with pytest.raises(ResumeLockError, match="live same-context"):
            acquire_same_context_lock(
                tmp_path,
                WORKFLOW_NAME,
                WORKFLOW_IDENTITY,
                WORKFLOW_SIGNATURE,
                process_inspector=FakeProcessInspector(200, "new", live=True),
            )
    finally:
        lock.release()


def test_empty_lock_directory_recovers_after_grace(tmp_path) -> None:
    lock_name = build_lock_name(WORKFLOW_NAME, WORKFLOW_IDENTITY, WORKFLOW_SIGNATURE)
    (tmp_path / "locks" / lock_name).mkdir(parents=True)

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        grace_seconds=0,
        process_inspector=FakeProcessInspector(200, "new"),
    )
    try:
        assert (lock.lock_dir / LOCK_OWNER_FILENAME).exists()
    finally:
        lock.release()


def test_ownerless_grace_restarts_for_recreated_lock(tmp_path, monkeypatch) -> None:
    lock_name = build_lock_name(WORKFLOW_NAME, WORKFLOW_IDENTITY, WORKFLOW_SIGNATURE)
    lock_dir = tmp_path / "locks" / lock_name
    lock_dir.mkdir(parents=True)
    times = iter([0.0, 0.2, 1.5])
    sleep_calls: list[float] = []

    def fake_monotonic() -> float:
        return next(times, 1.6)

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) == 1:
            shutil.rmtree(lock_dir)
            lock_dir.mkdir(parents=True)
            return
        owner = {
            "owner_token": "other-owner",
            "pid": 100,
            "hostname": "host",
            "process_start_identity": "other-start",
            "acquired_at": "2026-06-09T12:00:00",
            "workflow_identity": WORKFLOW_IDENTITY,
            "workflow_signature": WORKFLOW_SIGNATURE,
        }
        (lock_dir / LOCK_OWNER_FILENAME).write_text(
            json.dumps(owner),
            encoding="utf-8",
        )

    monkeypatch.setattr(resume_locks, "monotonic", fake_monotonic)
    monkeypatch.setattr(resume_locks, "sleep", fake_sleep)

    with pytest.raises(ResumeLockError, match="live same-context"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            grace_seconds=1.0,
            process_inspector=FakeProcessInspector(200, "new", live=True),
        )

    assert len(sleep_calls) == 2


def test_malformed_owner_lock_fails_closed_and_preserves_lock(tmp_path) -> None:
    lock_name = build_lock_name(WORKFLOW_NAME, WORKFLOW_IDENTITY, WORKFLOW_SIGNATURE)
    lock_dir = tmp_path / "locks" / lock_name
    lock_dir.mkdir(parents=True)
    (lock_dir / LOCK_OWNER_FILENAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(ResumeLockError, match="unexpected files"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            grace_seconds=0,
            process_inspector=FakeProcessInspector(200, "new"),
        )

    assert lock_dir.exists()


def test_unsupported_live_check_fails_closed(tmp_path) -> None:
    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    try:
        with pytest.raises(ResumeLockError, match="unsupported process check"):
            acquire_same_context_lock(
                tmp_path,
                WORKFLOW_NAME,
                WORKFLOW_IDENTITY,
                WORKFLOW_SIGNATURE,
                process_inspector=UnsafeProcessInspector(200, "new"),
            )
    finally:
        lock.release()
