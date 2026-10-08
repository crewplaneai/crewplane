from unittest.mock import Mock

import pytest

from crewplane.artifacts import locks
from crewplane.artifacts.locks import process_identity
from crewplane.artifacts.naming import build_lock_name


@pytest.mark.parametrize(
    "entry", ["ownerless", "malformed", "unexpected", "file", "valid"]
)
def test_existing_windows_lock_blocks_without_inspection(
    tmp_path, monkeypatch, entry
) -> None:
    lock_dir = tmp_path / "locks" / build_lock_name("work", "work.task.md", "signature")
    lock_dir.parent.mkdir()
    if entry == "file":
        lock_dir.write_bytes(b"block")
    elif entry == "valid":
        locks.acquire_same_context_lock(tmp_path, "work", "work.task.md", "signature")
    else:
        lock_dir.mkdir()
        if entry != "ownerless":
            (
                lock_dir / ("owner.json" if entry == "malformed" else "unexpected")
            ).write_bytes(b"?")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(locks, "is_native_windows", lambda: True)
    forbidden = Mock(
        side_effect=AssertionError("existing Windows lock must block immediately")
    )
    for name in ("_read_owner", "_recover_or_raise", "sleep"):
        monkeypatch.setattr(locks, name, forbidden)
    with pytest.raises(
        locks.ResumeLockError, match="Automatic lock recovery is unsupported"
    ) as error:
        locks.acquire_same_context_lock(tmp_path, "work", "work.task.md", "signature")
    assert str(lock_dir) in str(error.value)
    forbidden.assert_not_called()
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert locks.run_lock_activity(tmp_path, "work--run") == "unverifiable"


def test_windows_process_inspection_never_probes_pid(monkeypatch) -> None:
    monkeypatch.setattr(process_identity, "is_native_windows", lambda: True)
    probe = Mock(side_effect=AssertionError("POSIX probe on Windows"))
    monkeypatch.setattr(process_identity.os, "kill", probe)
    inspector = process_identity.ProcessInspector()
    identity = inspector.current()
    assert identity.start_identity is None
    for operation, argument in (
        (inspector.is_live, identity),
        (inspector.is_process_group_live, 1),
    ):
        with pytest.raises(RuntimeError, match="unsupported on native Windows"):
            operation(argument)
    probe.assert_not_called()
