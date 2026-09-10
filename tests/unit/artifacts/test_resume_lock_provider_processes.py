from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from typing import Literal

import pytest

from crewplane.artifacts.locks import (
    ResumeLockError,
    acquire_same_context_lock,
)
from crewplane.artifacts.locks.process_identity import ProcessIdentity
from crewplane.artifacts.naming import build_provider_process_state_filename
from crewplane.core.execution_state import RUN_STATE_SCHEMA_VERSION
from crewplane.core.provider_process_state import ProviderProcessState
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    make_run_manifest,
    write_run_manifest,
)
from tests.helpers.resume_locks import (
    FakeProcessInspector,
)


def _write_provider_process_state(
    state_dir: Path,
    status: Literal["started", "exited"] = "started",
    pid: int = 300,
    filename: str | None = None,
) -> Path:
    state = ProviderProcessState(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        run_id="source",
        run_key_name="workflow--source",
        node_id="build.node",
        task_id="codex_executor_0",
        provider="codex",
        role="executor",
        audit_round_num=None,
        round_num=1,
        attempt=1,
        pid=pid,
        process_group_id=pid,
        hostname="host",
        process_start_identity="provider-start",
        status=status,
        started_at=datetime(2026, 8, 7, 12, 0).isoformat(),
        exited_at=(
            datetime(2026, 8, 7, 12, 1).isoformat() if status == "exited" else None
        ),
        returncode=0 if status == "exited" else None,
    )
    process_dir = (
        state_dir
        / "execution-stages"
        / state.run_key_name
        / "manifests"
        / "provider-processes"
    )
    process_dir.mkdir(parents=True, exist_ok=True)
    state_filename = filename or build_provider_process_state_filename(
        state.node_id,
        state.task_id,
        state.provider,
        state.role,
        state.audit_round_num,
        state.round_num,
        state.attempt,
    )
    state_path = process_dir / state_filename
    state_path.write_text(
        state.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )
    return state_path


def _atomic_temp_path(state_path: Path) -> Path:
    return state_path.with_name(f".{state_path.name}.interrupted.tmp")


def _write_interrupted_exit_state(state_path: Path) -> Path:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": "exited",
            "exited_at": datetime(2026, 8, 7, 12, 1).isoformat(),
            "returncode": 0,
        }
    )
    temp_path = _atomic_temp_path(state_path)
    temp_path.write_text(json.dumps(payload), encoding="utf-8")
    return temp_path


def test_stale_lock_takeover_blocks_live_provider_process(tmp_path) -> None:
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
    _write_provider_process_state(tmp_path)

    with pytest.raises(ResumeLockError, match="provider process.*still active"):
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

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    assert stale.lock_dir.exists()
    stale.release()


def test_live_provider_check_keeps_lock_visible_to_concurrent_acquirer(
    tmp_path,
) -> None:
    provider_check_started = Event()
    release_provider_check = Event()
    recovery_errors: list[BaseException] = []

    class BlockingProviderInspector(FakeProcessInspector):
        def is_live(self, identity: ProcessIdentity) -> bool:
            if identity.pid != 300:
                return False
            provider_check_started.set()
            if not release_provider_check.wait(timeout=2):
                raise TimeoutError("provider process check was not released")
            return True

    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    _write_provider_process_state(tmp_path, pid=300)

    def recover_stale_lock() -> None:
        try:
            acquire_same_context_lock(
                tmp_path,
                WORKFLOW_NAME,
                WORKFLOW_IDENTITY,
                WORKFLOW_SIGNATURE,
                process_inspector=BlockingProviderInspector(200, "new"),
            )
        except BaseException as exc:
            recovery_errors.append(exc)

    recovery_thread = Thread(target=recover_stale_lock, daemon=True)
    recovery_thread.start()
    contender = None
    try:
        assert provider_check_started.wait(timeout=2)
        assert stale.lock_dir.exists()

        with pytest.raises(ResumeLockError, match="provider process.*still active"):
            contender = acquire_same_context_lock(
                tmp_path,
                WORKFLOW_NAME,
                WORKFLOW_IDENTITY,
                WORKFLOW_SIGNATURE,
                process_inspector=FakeProcessInspector(
                    400,
                    "contender",
                    live_checks=[False, True],
                ),
            )

        assert stale.lock_dir.exists()
        assert not tuple(stale.lock_dir.parent.glob(f"{stale.lock_dir.name}.recover-*"))
    finally:
        if contender is not None:
            contender.release()
        release_provider_check.set()
        recovery_thread.join(timeout=2)
        stale.release()

    assert not recovery_thread.is_alive()
    assert len(recovery_errors) == 1
    assert isinstance(recovery_errors[0], ResumeLockError)
    assert "provider process" in str(recovery_errors[0])


def test_stale_lock_takeover_allows_dead_provider_process(tmp_path) -> None:
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
    _write_provider_process_state(tmp_path)

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(
            200,
            "new",
            live_checks=[False, False, False],
        ),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "cancelled"
        assert manifest["cancel_reason"] == "stale_lock_recovered"
    finally:
        lock.release()


def test_stale_lock_takeover_allows_dead_exited_provider_process(tmp_path) -> None:
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
    _write_provider_process_state(tmp_path, status="exited")

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(
            200,
            "new",
            live_checks=[False, False, False],
        ),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "cancelled"
    finally:
        lock.release()


def test_stale_lock_takeover_allows_interrupted_provider_exit_publication(
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
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    state_path = _write_provider_process_state(tmp_path)
    _write_interrupted_exit_state(state_path)

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(
            200,
            "new",
            live_checks=[False, False, False],
        ),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "cancelled"
    finally:
        lock.release()


def test_stale_lock_takeover_rejects_mismatched_interrupted_exit_state(
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
    write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    state_path = _write_provider_process_state(tmp_path)
    temp_path = _write_interrupted_exit_state(state_path)
    payload = json.loads(temp_path.read_text(encoding="utf-8"))
    payload["pid"] = 301
    temp_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ResumeLockError, match="does not match its published receipt"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(200, "new", live_checks=[False]),
        )

    assert stale.lock_dir.exists()
    stale.release()


def test_stale_lock_takeover_allows_interrupted_provider_start_publication(
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
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    state_path = _write_provider_process_state(tmp_path)
    os.link(state_path, _atomic_temp_path(state_path))

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(
            200,
            "new",
            live_checks=[False, False, False],
        ),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "cancelled"
    finally:
        lock.release()


def test_stale_lock_takeover_rejects_temp_only_provider_process_state(
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
    write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    state_path = _write_provider_process_state(tmp_path)
    state_path.replace(_atomic_temp_path(state_path))

    with pytest.raises(ResumeLockError, match="safe file"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(
                200,
                "new",
                live_checks=[False, False],
            ),
        )

    assert stale.lock_dir.exists()
    stale.release()


def test_stale_lock_takeover_blocks_live_process_with_exited_receipt(tmp_path) -> None:
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
    _write_provider_process_state(tmp_path, status="exited")

    with pytest.raises(ResumeLockError, match="exited receipt"):
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

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    assert stale.lock_dir.exists()
    stale.release()


def test_stale_lock_takeover_blocks_live_provider_process_group(tmp_path) -> None:
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
    _write_provider_process_state(tmp_path, status="exited")

    with pytest.raises(ResumeLockError, match="process group may still be active"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(
                200,
                "new",
                live_checks=[False, False],
                group_live=True,
            ),
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    assert stale.lock_dir.exists()
    stale.release()


def test_stale_lock_takeover_fails_when_provider_process_is_unverifiable(
    tmp_path,
) -> None:
    class ProviderUnsafeInspector(FakeProcessInspector):
        def __init__(self) -> None:
            super().__init__(200, "new")
            self.check_count = 0

        def is_live(self, identity: ProcessIdentity) -> bool:
            del identity
            self.check_count += 1
            if self.check_count == 2:
                raise RuntimeError("process identity unavailable")
            return False

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
    _write_provider_process_state(tmp_path)

    with pytest.raises(ResumeLockError, match="Cannot safely verify a provider"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=ProviderUnsafeInspector(),
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    assert stale.lock_dir.exists()
    stale.release()


def test_stale_lock_takeover_fails_closed_on_malformed_provider_process_state(
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
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    state_path = _write_provider_process_state(tmp_path)
    state_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ResumeLockError, match="malformed or unreadable"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(
                200,
                "new",
                live_checks=[False, False],
            ),
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    assert stale.lock_dir.exists()
    stale.release()


@pytest.mark.parametrize(
    ("link_kind", "expected_message"),
    [("symlink", "symlink"), ("hardlink", "safe file")],
)
def test_stale_lock_takeover_rejects_linked_provider_process_state(
    tmp_path,
    link_kind: str,
    expected_message: str,
) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    state_path = _write_provider_process_state(tmp_path)
    outside_path = tmp_path / "outside-provider-process.json"
    state_path.replace(outside_path)
    try:
        if link_kind == "symlink":
            state_path.symlink_to(outside_path)
        else:
            os.link(outside_path, state_path)
    except OSError as exc:
        stale.release()
        pytest.skip(f"{link_kind} creation is unavailable: {exc}")

    with pytest.raises(ResumeLockError, match=expected_message):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(
                200,
                "new",
                live_checks=[False, False],
            ),
        )

    assert stale.lock_dir.exists()
    stale.release()


def test_stale_lock_takeover_rejects_mismatched_provider_process_state(
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
    write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    state_path = _write_provider_process_state(tmp_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["run_id"] = "other-run"
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ResumeLockError, match="does not match"):
        acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            process_inspector=FakeProcessInspector(
                200,
                "new",
                live_checks=[False, False],
            ),
        )

    assert stale.lock_dir.exists()
    stale.release()
