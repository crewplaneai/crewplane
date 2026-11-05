from __future__ import annotations

import json
from collections.abc import Callable
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
    def __init__(
        self,
        transaction_id: str,
        observed: Literal["published", "not_published", "unknown"],
        cause: OSError,
    ):
        self.transaction_id = transaction_id
        self.observed = observed
        super().__init__(
            f"Memory publication durability is uncertain ({cause}); transaction {transaction_id}: {observed} on protected readback. Inspect current state before retrying; do not blindly repeat acceptance."
        )


class MemoryStore:
    def __init__(self, project_root: Path, lock_timeout_seconds: float = 10.0):
        self.project_root = project_root
        self.directory = project_root / ".crewplane" / "memory"
        self.path = self.directory / "store.json"
        self.lock_path = self.directory / "store.lock"
        self.lock_timeout_seconds = lock_timeout_seconds

    async def initialize(self) -> MemorySnapshot:
        ensure_contained_directory(self.project_root, ".crewplane/memory")
        if (
            self.path.exists() or self.path.is_symlink()
        ) and not self.lock_path.exists():
            raise ValueError(
                "Memory store exists without its persistent lock; restore the original lock/state together, not an implicit replacement."
            )
        ensure_single_link_regular_file(self.lock_path)
        async with memory_lock(self.lock_path, True, self.lock_timeout_seconds):
            if self.path.exists() or self.path.is_symlink():
                return self._read()
            snapshot = MemorySnapshot()
            self._publish(snapshot, absent=True)
            return snapshot

    async def snapshot(self) -> MemorySnapshot:
        self._require_initialized()
        async with memory_lock(self.lock_path, False, self.lock_timeout_seconds):
            return self._read()

    async def transact[T](
        self, operation: Callable[[MemorySnapshot], tuple[MemorySnapshot, T]]
    ) -> T:
        self._require_initialized()
        async with memory_lock(self.lock_path, True, self.lock_timeout_seconds):
            current = self._read()
            updated, result = operation(current)
            validated = MemorySnapshot.model_validate(updated.model_dump(mode="json"))
            enforce_bounds(validated)
            if validated != current:
                if validated.transaction_id == current.transaction_id:
                    raise ValueError(
                        "A memory mutation must identify a new transaction."
                    )
                self._publish(validated)
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
    for record in snapshot.records:
        if len(json_bytes(record.model_dump(mode="json"))) > RECORD_LIMIT_BYTES:
            raise ValueError(
                f"Memory record {record.record_id} exceeds the {RECORD_LIMIT_BYTES}-byte limit including retained revisions; no history was trimmed."
            )
    if len(json_bytes(snapshot.model_dump(mode="json"))) > STORE_LIMIT_BYTES:
        raise ValueError(
            f"Memory store exceeds the {STORE_LIMIT_BYTES}-byte limit; no history was trimmed."
        )
