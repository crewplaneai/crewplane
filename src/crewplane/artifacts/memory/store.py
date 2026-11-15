"""Protected, size-limited project memory with explicit initialization.

Reads share the persistent lock; initialization and transactions hold it
exclusively. Store I/O runs off the event loop and finishes before cancellation
can release a held lock. Transaction callbacks remain synchronous on the caller's
event-loop thread. Publication errors can follow a visible write: inspect their
protected readback before deciding whether to retry.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextvars import copy_context
from functools import partial
from pathlib import Path
from typing import Literal

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import (
    contained_directory,
    contained_regular_file,
    ensure_contained_directory,
    ensure_single_link_regular_file,
)
from crewplane.artifacts.atomic import (
    atomic_write_json,
    atomic_write_json_if_absent,
    json_bytes,
)
from crewplane.core.memory.models import MemorySnapshot

from .locking import memory_lock

STORE_LIMIT_BYTES = 16 * 1024 * 1024
RECORD_LIMIT_BYTES = 32 * 1024


class MemoryPublicationError(OSError):
    """Report an uncertain save without replaying or rolling back the transaction.

    Attributes:
        transaction_id: Identifier of the attempted snapshot.
        observed: Protected readback result: ``published`` means the attempted
            snapshot was visible, ``not_published`` means it was absent or
            different, and ``unknown`` means readback could not validate it.
            Visibility does not prove durability or that an earlier write was
            never visible. Inspect current state before retrying acceptance.
    """

    def __init__(
        self,
        transaction_id: str,
        observed: Literal["published", "not_published", "unknown"],
        cause: OSError,
    ) -> None:
        """Retain the attempted transaction and readback status for recovery."""
        self.transaction_id = transaction_id
        self.observed = observed
        super().__init__(
            f"Memory publication durability is uncertain ({cause}); transaction {transaction_id}: {observed} on protected readback. Inspect current state before retrying; do not blindly repeat acceptance."
        )


class MemoryStore:
    """Coordinate a project's JSON memory through its persistent flock lock.

    Only ``initialize`` creates the store or lock. Other operations reject
    missing or unsafe state; existing state is never implicitly repaired.
    Cooperating readers and writers must use the same persistent lock file.
    """

    def __init__(self, project_root: Path, lock_timeout_seconds: float = 10.0) -> None:
        """Bind paths without I/O and set the lock acquisition timeout in seconds."""
        self.project_root = project_root
        self.directory = project_root / ".crewplane" / "memory"
        self.path = self.directory / "store.json"
        self.lock_path = self.directory / "store.lock"
        self.lock_timeout_seconds = lock_timeout_seconds

    async def initialize(self) -> MemorySnapshot:
        """Create an empty store or validate and return the existing snapshot.

        Reuse the persistent lock, including after interrupted initialization.
        Existing store data without its lock raises ValueError. Publication may
        raise MemoryPublicationError after the new snapshot becomes visible.
        Cancellation waits for active I/O to finish and may leave initialized
        state; any I/O failure is retained as the cancellation's cause.
        """
        await _complete_store_io(self._prepare_initialization)
        async with memory_lock(self.lock_path, True, self.lock_timeout_seconds):
            return await _complete_store_io(self._initialize_locked)

    def _prepare_initialization(self) -> None:
        ensure_contained_directory(self.project_root, ".crewplane/memory")
        if (
            self.path.exists() or self.path.is_symlink()
        ) and not self.lock_path.exists():
            raise ValueError(
                "Memory store exists without its persistent lock; restore the original lock/state together, not an implicit replacement."
            )
        ensure_single_link_regular_file(self.lock_path)

    def _initialize_locked(self) -> MemorySnapshot:
        if self.path.exists() or self.path.is_symlink():
            return self._read()
        snapshot = MemorySnapshot()
        self._publish(snapshot, absent=True)
        return snapshot

    async def snapshot(self) -> MemorySnapshot:
        """Return one validated snapshot under a shared lock without creating files.

        Require explicit initialization. Missing, unsafe, malformed, or oversized
        state raises ValueError or the underlying filesystem error. Cancellation
        waits for active I/O before releasing the lock and retains any I/O
        failure as its cause.
        """
        await _complete_store_io(self._require_initialized)
        async with memory_lock(self.lock_path, False, self.lock_timeout_seconds):
            return await _complete_store_io(self._read)

    async def transact[T](
        self, operation: Callable[[MemorySnapshot], tuple[MemorySnapshot, T]]
    ) -> T:
        """Apply a synchronous callback once under an exclusive lock and return T.

        Require explicit initialization and reload current state under the lock.
        Run ``operation(current)`` on the caller's event-loop thread and context;
        it must return ``(updated_snapshot, result)`` and avoid long-running work.
        Revalidate the update and enforce byte limits even for an unchanged
        snapshot. Changed state must use a different transaction ID; equal state
        skips publication. Callback and validation errors propagate without a
        write. MemoryPublicationError can follow a visible write and requires
        inspection before retrying; callbacks are never automatically replayed.

        Cancellation waits for active I/O, including protected readback and
        worker cleanup, before releasing the lock. A cancelled call can therefore
        have committed; any I/O failure is retained as the cancellation's cause.
        """
        await _complete_store_io(self._require_initialized)
        async with memory_lock(self.lock_path, True, self.lock_timeout_seconds):
            current = await _complete_store_io(self._read)
            updated, result = operation(current)
            validated = MemorySnapshot.model_validate(updated.model_dump(mode="json"))
            enforce_bounds(validated)
            if validated != current:
                if validated.transaction_id == current.transaction_id:
                    raise ValueError(
                        "A memory mutation must identify a new transaction."
                    )
                await _complete_store_io(partial(self._publish, validated))
            return result

    def _require_initialized(self) -> None:
        if contained_directory(self.project_root, ".crewplane/memory") is None:
            raise ValueError(
                "Project memory is missing or unsafe; run 'crewplane memory init' explicitly."
            )
        if contained_regular_file(self.directory, "store.lock") is None:
            raise ValueError(
                "Project memory persistent lock is missing or unsafe; existing state must not be repaired implicitly."
            )
        if contained_regular_file(self.directory, "store.json") is None:
            raise ValueError(
                "Project memory store is missing or unsafe; run 'crewplane memory init' for an interrupted initialization."
            )

    def _read(self) -> MemorySnapshot:
        payload = json.loads(
            read_contained_bytes(
                self.project_root, ".crewplane/memory/store.json", STORE_LIMIT_BYTES
            )
        )
        snapshot = MemorySnapshot.model_validate(payload)
        if payload != snapshot.model_dump(mode="json"):
            raise ValueError(
                "Memory store has an incomplete or malformed current shape."
            )
        enforce_bounds(snapshot)
        return snapshot

    def _publish(self, snapshot: MemorySnapshot, absent: bool = False) -> None:
        enforce_bounds(snapshot)
        try:
            writer = atomic_write_json_if_absent if absent else atomic_write_json
            writer(self.path, snapshot.model_dump(mode="json"), ensure_parent=False)
        except OSError as exc:
            observed: Literal["published", "not_published", "unknown"] = "unknown"
            try:
                actual = self._read()
                observed = "published" if actual == snapshot else "not_published"
            except FileNotFoundError:
                observed = "not_published"
            except (OSError, ValueError):
                pass
            raise MemoryPublicationError(
                snapshot.transaction_id, observed, exc
            ) from exc


def enforce_bounds(snapshot: MemorySnapshot) -> None:
    """Reject oversized records or stores with ValueError, without trimming history.

    Limits count the serialized JSON bytes, including all retained revisions.
    Record limits are checked in record order before the aggregate store limit.
    """
    for record in snapshot.records:
        if len(json_bytes(record.model_dump(mode="json"))) > RECORD_LIMIT_BYTES:
            raise ValueError(
                f"Memory record {record.record_id} exceeds the {RECORD_LIMIT_BYTES}-byte limit including retained revisions; no history was trimmed."
            )
    if len(json_bytes(snapshot.model_dump(mode="json"))) > STORE_LIMIT_BYTES:
        raise ValueError(
            f"Memory store exceeds the {STORE_LIMIT_BYTES}-byte limit; no history was trimmed."
        )


async def _complete_store_io[T](operation: Callable[[], T]) -> T:
    """Finish store I/O before cancellation can release the caller's held lock."""
    worker = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, operation
    )
    cancelled: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.wait((worker,))
        except asyncio.CancelledError as exc:
            cancelled = exc
    try:
        result = worker.result()
    except Exception as exc:
        if cancelled is not None:
            raise cancelled from exc
        raise
    if cancelled is not None:
        raise cancelled
    return result
