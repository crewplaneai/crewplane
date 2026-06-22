from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from orchestrator_cli.core.preflight.models import WorkspaceSourceSnapshot

from ..state import (
    WorkspaceStateRetention,
    WorkspaceStateUpdateRequest,
    update_workspace_state,
)
from . import remove_worktree_workspace
from .types import WorktreeSourceRef


@dataclass(frozen=True)
class ReusableWorktreeCheckout:
    node_id: str
    logical_worktree_name: str
    workspace_path: Path
    checkout_root: Path
    cwd: Path
    git_dir: Path
    source_commit: str
    source_tree: str
    source: WorkspaceSourceSnapshot
    state_path: Path
    cleanup_on_success: bool


@dataclass(frozen=True)
class WorktreeReuseCleanupResult:
    errors: tuple[Exception, ...] = ()
    updated_state_paths: tuple[Path, ...] = ()


class WorktreeReuseCache:
    def __init__(self) -> None:
        self._entries: dict[str, ReusableWorktreeCheckout] = {}
        self._leased_entries: dict[Path, ReusableWorktreeCheckout] = {}
        self._failed_cleanup_entries: dict[Path, ReusableWorktreeCheckout] = {}
        self._state_paths_by_workspace: dict[Path, set[Path]] = {}
        self._pending_updated_state_paths: set[Path] = set()
        self._lock = Lock()

    def take(
        self,
        logical_worktree_name: str,
        source_ref: WorktreeSourceRef,
    ) -> ReusableWorktreeCheckout | None:
        with self._lock:
            entry = self._entries.pop(logical_worktree_name, None)
            if entry is None:
                return None
            if _matches_source(entry, source_ref) and entry.checkout_root.exists():
                entry_key = _workspace_key(entry.workspace_path)
                self._leased_entries[entry_key] = entry
                self._remember_state_path(entry_key, entry.state_path)
                return entry
        self.cleanup_entry_best_effort(entry)
        return None

    def store(self, entry: ReusableWorktreeCheckout) -> None:
        entry_key = _workspace_key(entry.workspace_path)
        with self._lock:
            previous = self._entries.get(entry.logical_worktree_name)
            leased = self._leased_entries.pop(entry_key, None)
            if leased is not None:
                self._remember_state_path(entry_key, leased.state_path)
            self._remember_state_path(entry_key, entry.state_path)
            self._entries[entry.logical_worktree_name] = entry
        if previous is not None and previous.workspace_path != entry.workspace_path:
            self.cleanup_entry_best_effort(previous)

    def owns(self, workspace_path: Path | None) -> bool:
        if workspace_path is None:
            return False
        resolved = workspace_path.resolve(strict=False)
        with self._lock:
            return (
                any(
                    entry.workspace_path.resolve(strict=False) == resolved
                    for entry in self._entries.values()
                )
                or resolved in self._leased_entries
                or resolved in self._failed_cleanup_entries
            )

    def discard_workspace(self, workspace_path: Path) -> None:
        resolved = _workspace_key(workspace_path)
        with self._lock:
            self._leased_entries.pop(resolved, None)
            stale_keys = [
                logical_worktree_name
                for logical_worktree_name, entry in self._entries.items()
                if _workspace_key(entry.workspace_path) == resolved
            ]
            for logical_worktree_name in stale_keys:
                self._entries.pop(logical_worktree_name, None)
            self._failed_cleanup_entries.pop(resolved, None)
            self._state_paths_by_workspace.pop(resolved, None)

    def cleanup_workspace(
        self,
        workspace_path: Path,
    ) -> tuple[Path, ...]:
        entry = self._entry_for_workspace(workspace_path)
        if entry is None:
            return ()
        state_paths = self._state_paths_for_entry(entry)
        remove_worktree_workspace(entry.source, entry.workspace_path)
        updated = _update_deleted_state_paths(state_paths)
        self._forget_entry(entry)
        self._remember_pending_updated_state_paths(updated)
        return updated

    def cleanup_workspace_best_effort(
        self,
        workspace_path: Path,
    ) -> Exception | None:
        try:
            self.cleanup_workspace(workspace_path)
        except Exception as exc:
            return exc
        return None

    def cleanup_entry(
        self,
        entry: ReusableWorktreeCheckout,
    ) -> tuple[Path, ...]:
        state_paths = self._state_paths_for_entry(entry)
        if entry.cleanup_on_success:
            remove_worktree_workspace(entry.source, entry.workspace_path)
            updated = _update_deleted_state_paths(state_paths)
            self._forget_entry(entry)
            return updated
        self._forget_entry(entry)
        return ()

    def cleanup_entry_best_effort(
        self,
        entry: ReusableWorktreeCheckout,
    ) -> Exception | None:
        try:
            self._remember_pending_updated_state_paths(self.cleanup_entry(entry))
        except Exception as exc:
            self._retain_failed_cleanup_entry(entry)
            return exc
        return None

    def cleanup_all_best_effort(self) -> tuple[Exception, ...]:
        return self.cleanup_all().errors

    def cleanup_all(self) -> WorktreeReuseCleanupResult:
        with self._lock:
            entries = _unique_entries(
                (
                    *self._entries.values(),
                    *self._leased_entries.values(),
                    *self._failed_cleanup_entries.values(),
                )
            )
            pending_state_paths = self._pending_updated_state_paths
            self._pending_updated_state_paths = set()
        errors: list[Exception] = []
        updated_state_paths: list[Path] = list(pending_state_paths)
        for entry in entries:
            try:
                updated_state_paths.extend(self.cleanup_entry(entry))
            except Exception as exc:
                self._retain_failed_cleanup_entry(entry)
                errors.append(exc)
        return WorktreeReuseCleanupResult(
            errors=tuple(errors),
            updated_state_paths=tuple(_sorted_unique_paths(updated_state_paths)),
        )

    def cleanup_node_best_effort(self, node_id: str) -> tuple[Exception, ...]:
        with self._lock:
            entries = tuple(
                entry
                for entry in _unique_entries(
                    (
                        *self._entries.values(),
                        *self._failed_cleanup_entries.values(),
                    )
                )
                if entry.node_id == node_id
            )
        errors: list[Exception] = []
        for entry in entries:
            if exc := self.cleanup_entry_best_effort(entry):
                errors.append(exc)
        return tuple(errors)

    def _forget_entry(self, entry: ReusableWorktreeCheckout) -> set[Path]:
        entry_key = _workspace_key(entry.workspace_path)
        with self._lock:
            cached_entry = self._entries.get(entry.logical_worktree_name)
            if (
                cached_entry is not None
                and cached_entry.workspace_path == entry.workspace_path
            ):
                self._entries.pop(entry.logical_worktree_name, None)
            self._leased_entries.pop(entry_key, None)
            self._failed_cleanup_entries.pop(entry_key, None)
            state_paths = self._state_paths_by_workspace.pop(entry_key, set())
        state_paths.add(entry.state_path)
        return state_paths

    def _state_paths_for_entry(self, entry: ReusableWorktreeCheckout) -> set[Path]:
        entry_key = _workspace_key(entry.workspace_path)
        with self._lock:
            state_paths = set(self._state_paths_by_workspace.get(entry_key, set()))
        state_paths.add(entry.state_path)
        return state_paths

    def _entry_for_workspace(
        self,
        workspace_path: Path,
    ) -> ReusableWorktreeCheckout | None:
        entry_key = _workspace_key(workspace_path)
        with self._lock:
            leased = self._leased_entries.get(entry_key)
            if leased is not None:
                return leased
            failed = self._failed_cleanup_entries.get(entry_key)
            if failed is not None:
                return failed
            for entry in self._entries.values():
                if _workspace_key(entry.workspace_path) == entry_key:
                    return entry
        return None

    def _retain_failed_cleanup_entry(self, entry: ReusableWorktreeCheckout) -> None:
        with self._lock:
            self._failed_cleanup_entries[_workspace_key(entry.workspace_path)] = entry

    def _remember_state_path(self, entry_key: Path, state_path: Path) -> None:
        self._state_paths_by_workspace.setdefault(entry_key, set()).add(state_path)

    def _remember_pending_updated_state_paths(self, paths: tuple[Path, ...]) -> None:
        if not paths:
            return
        with self._lock:
            self._pending_updated_state_paths.update(paths)


def _matches_source(
    entry: ReusableWorktreeCheckout,
    source_ref: WorktreeSourceRef,
) -> bool:
    return (
        entry.source_commit == source_ref.source_commit
        and entry.source_tree == source_ref.source_tree
    )


def _workspace_key(workspace_path: Path) -> Path:
    return workspace_path.resolve(strict=False)


def _unique_entries(
    entries: tuple[ReusableWorktreeCheckout, ...],
) -> tuple[ReusableWorktreeCheckout, ...]:
    unique: dict[Path, ReusableWorktreeCheckout] = {}
    for entry in entries:
        unique[_workspace_key(entry.workspace_path)] = entry
    return tuple(unique.values())


def _update_deleted_state_paths(state_paths: set[Path]) -> tuple[Path, ...]:
    ordered = _sorted_unique_paths(state_paths)
    for state_path in ordered:
        update_workspace_state(
            state_path,
            WorkspaceStateUpdateRequest(
                status="succeeded",
                retention=WorkspaceStateRetention(
                    retention="deleted",
                    retained_reason=None,
                ),
            ),
        )
    return ordered


def _sorted_unique_paths(paths: set[Path] | list[Path]) -> tuple[Path, ...]:
    return tuple(sorted(set(paths), key=lambda path: path.as_posix()))
