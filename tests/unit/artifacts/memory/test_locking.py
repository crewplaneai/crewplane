import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from crewplane.artifacts.memory import locking


@pytest.mark.parametrize("exclusive", [False, True])
def test_memory_lock_rejects_native_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exclusive: bool
) -> None:
    lock_path = tmp_path / "store.lock"
    lock_path.write_bytes(b"persistent lock")
    monkeypatch.setattr(locking, "os", SimpleNamespace(name="nt"))

    async def acquire() -> None:
        async with locking.memory_lock(lock_path, exclusive):
            pytest.fail("Unsupported memory lock was acquired")

    with pytest.raises(ValueError, match="Linux, macOS, or WSL flock semantics"):
        asyncio.run(acquire())

    assert lock_path.read_bytes() == b"persistent lock"
