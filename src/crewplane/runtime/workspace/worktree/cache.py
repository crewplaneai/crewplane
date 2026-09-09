from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from crewplane.architecture.safe_files import path_is_absent
from crewplane.core.preflight.models import WorkspaceSourceSnapshot

from ..state import (
    WorkspaceStateRetention,
    update_workspace_retention,
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
    repository_id: str
    run_key_name: str
    reuse_generation: int = 1


@dataclass(frozen=True)
class WorktreeReuseCleanupResult:
    errors: tuple[Exception, ...] = ()
    updated_state_paths: tuple[Path, ...] = ()


class WorktreeReuseCache:
    def __init__(self) -> None:
        self._entries: dict[
            tuple[str, str, str],
            ReusableWorktreeCheckout,
        ] = {}
        self._leased_entries: dict[Path, ReusableWorktreeCheckout] = {}
        self._failed_cleanup_entries: dict[Path, ReusableWorktreeCheckout] = {}
        self._state_paths_by_workspace: dict[Path, set[Path]] = {}
        self._physically_removed_workspaces: set[Path] = set()
        self._pending_updated_state_paths: set[Path] = set()
        self._lock = Lock()

    def take(
        self,
        logical_worktree_name: str,
        source_ref: WorktreeSourceRef,
        repository_id: str,
        run_key_name: str,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> ReusableWorktreeCheckout | None:
        cache_key = (repository_id, run_key_name, logical_worktree_name)
        with self._lock:
            entry = self._entries.pop(cache_key, None)
            if entry is None:
                return None
            if _matches_source(entry, source_ref) and entry.checkout_root.exists():
                entry_key = _workspace_key(entry.workspace_path)
                self._leased_entries[entry_key] = entry
                self._remember_state_path(entry_key, entry.state_path)
                return entry
        self.cleanup_entry_best_effort(entry, cancel_requested)
        return None

    def store(
        self,
        entry: ReusableWorktreeCheckout,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        entry_key = _workspace_key(entry.workspace_path)
        cache_key = _entry_cache_key(entry)
        with self._lock:
            previous = self._entries.get(cache_key)
            leased = self._leased_entries.pop(entry_key, None)
            if leased is not None:
                self._remember_state_path(entry_key, leased.state_path)
            self._remember_state_path(entry_key, entry.state_path)
            self._entries[cache_key] = entry
        if previous is not None and previous.workspace_path != entry.workspace_path:
            self.cleanup_entry_best_effort(previous, cancel_requested)

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
                cache_key
                for cache_key, entry in self._entries.items()
                if _workspace_key(entry.workspace_path) == resolved
            ]
            for cache_key in stale_keys:
                self._entries.pop(cache_key, None)
            self._failed_cleanup_entries.pop(resolved, None)
            self._state_paths_by_workspace.pop(resolved, None)
            self._physically_removed_workspaces.discard(resolved)

    def cleanup_workspace(
        self,
        workspace_path: Path,
        cancel_requested: Callable[[], bool] | None = None,
        state_path: Path | None = None,
    ) -> tuple[Path, ...]:
        entry = self._entry_for_workspace(workspace_path)
        if entry is None:
            return ()
        if state_path is not None:
            with self._lock:
                self._remember_state_path(
                    _workspace_key(entry.workspace_path), state_path
                )
        state_paths = self._state_paths_for_entry(entry)
        try:
            self._remove_entry_workspace(entry, cancel_requested)
            updated = _update_deleted_state_paths(state_paths)
        except Exception:
            self._retain_failed_cleanup_entry(entry)
            raise
        self._forget_entry(entry)
        self._remember_pending_updated_state_paths(updated)
        return updated

    def defer_workspace_cleanup(self, workspace_path: Path, state_path: Path) -> bool:
        """Release an owned checkout for cleanup after invocation drift checks."""
        entry = self._entry_for_workspace(workspace_path)
        if entry is None:
            return False
        with self._lock:
            self._remember_state_path(_workspace_key(entry.workspace_path), state_path)
        self._retain_failed_cleanup_entry(entry)
        return True

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
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[Path, ...]:
        state_paths = self._state_paths_for_entry(entry)
        if entry.cleanup_on_success:
            self._remove_entry_workspace(entry, cancel_requested)
            updated = _update_deleted_state_paths(state_paths)
            self._forget_entry(entry)
            return updated
        self._forget_entry(entry)
        return ()

    def cleanup_entry_best_effort(
        self,
        entry: ReusableWorktreeCheckout,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Exception | None:
        try:
            self._remember_pending_updated_state_paths(
                self.cleanup_entry(entry, cancel_requested)
            )
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
                    *self._failed_cleanup_entries.values(),
                )
            )
            unresolved_leases = _unique_entries(tuple(self._leased_entries.values()))
            pending_state_paths = self._pending_updated_state_paths
            self._pending_updated_state_paths = set()
        errors: list[Exception] = [
            RuntimeError(
                "Reusable workspace remains leased by an unresolved invocation: "
                f"{entry.workspace_path.as_posix()}."
            )
            for entry in unresolved_leases
        ]
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
            cache_key = _entry_cache_key(entry)
            cached_entry = self._entries.get(cache_key)
            if (
                cached_entry is not None
                and cached_entry.workspace_path == entry.workspace_path
            ):
                self._entries.pop(cache_key, None)
            self._leased_entries.pop(entry_key, None)
            self._failed_cleanup_entries.pop(entry_key, None)
            state_paths = self._state_paths_by_workspace.pop(entry_key, set())
            self._physically_removed_workspaces.discard(entry_key)
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
            entry_key = _workspace_key(entry.workspace_path)
            self._leased_entries.pop(entry_key, None)
            self._failed_cleanup_entries[entry_key] = entry

    def _remove_entry_workspace(
        self,
        entry: ReusableWorktreeCheckout,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        entry_key = _workspace_key(entry.workspace_path)
        with self._lock:
            removal_completed = entry_key in self._physically_removed_workspaces
        if removal_completed:
            if not path_is_absent(entry.workspace_path):
                raise RuntimeError(
                    "Reusable workspace reappeared after physical cleanup."
                )
            return
        if path_is_absent(entry.workspace_path):
            raise RuntimeError(
                "Reusable workspace is absent without completed cleanup evidence."
            )
        if cancel_requested is None:
            remove_worktree_workspace(
                entry.source,
                entry.workspace_path,
                entry.git_dir,
            )
        else:
            remove_worktree_workspace(
                entry.source,
                entry.workspace_path,
                entry.git_dir,
                cancel_requested,
            )
        if not path_is_absent(entry.workspace_path):
            raise RuntimeError("Reusable workspace cleanup left its path present.")
        with self._lock:
            self._physically_removed_workspaces.add(entry_key)

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


def _entry_cache_key(
    entry: ReusableWorktreeCheckout,
) -> tuple[str, str, str]:
    if not entry.repository_id or not entry.run_key_name:
        raise RuntimeError("Reusable workspace lacks repository and run ownership.")
    return (
        entry.repository_id,
        entry.run_key_name,
        entry.logical_worktree_name,
    )


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
        update_workspace_retention(
            state_path,
            WorkspaceStateRetention(
                retention="deleted",
                retained_reason=None,
            ),
        )
    return ordered


def _sorted_unique_paths(paths: set[Path] | list[Path]) -> tuple[Path, ...]:
    return tuple(sorted(set(paths), key=lambda path: path.as_posix()))
