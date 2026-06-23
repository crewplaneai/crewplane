from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GeneratedFileWorkspaceCleanupResult:
    errors: tuple[Exception, ...] = ()
    cleaned_node_ids: tuple[str, ...] = ()


@dataclass
class GeneratedFileWorkspaceRegistry:
    roots_by_node: dict[str, dict[Path, Path]] = field(default_factory=dict)
    cleanup_by_node: dict[str, list[Callable[[], None]]] = field(default_factory=dict)

    def record(
        self,
        node_id: str,
        output_file: Path,
        workspace_root: Path | None,
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        if workspace_root is not None:
            self.roots_by_node.setdefault(node_id, {})[
                output_file.resolve(strict=False)
            ] = workspace_root.resolve(strict=False)
        if cleanup is not None:
            self.cleanup_by_node.setdefault(node_id, []).append(cleanup)

    def roots_for_node(self, node_id: str) -> dict[Path, Path]:
        return dict(self.roots_by_node.get(node_id, {}))

    def alias_output_file(
        self,
        node_id: str,
        source_output_file: Path,
        alias_output_file: Path,
    ) -> None:
        roots = self.roots_by_node.get(node_id)
        if roots is None:
            return
        workspace_root = roots.get(source_output_file.resolve(strict=False))
        if workspace_root is None:
            return
        roots[alias_output_file.resolve(strict=False)] = workspace_root

    def cleanup_node(self, node_id: str) -> None:
        errors = self.cleanup_node_best_effort(node_id)
        if not errors:
            return
        raise RuntimeError(
            f"Generated-file workspace cleanup failed for node '{node_id}' "
            f"({len(errors)} callback(s) failed)."
        ) from errors[0]

    async def cleanup_node_best_effort_async(
        self,
        node_id: str,
        retain_failed_callbacks: bool = True,
    ) -> tuple[Exception, ...]:
        callbacks = list(self.cleanup_by_node.get(node_id, []))
        failed_callbacks, errors, _ = await asyncio.to_thread(
            self._run_callbacks,
            callbacks,
        )
        self._record_node_cleanup_result(
            node_id,
            failed_callbacks,
            errors,
            retain_failed_callbacks,
        )
        return errors

    def cleanup_node_best_effort(
        self,
        node_id: str,
        retain_failed_callbacks: bool = True,
    ) -> tuple[Exception, ...]:
        callbacks = self.cleanup_by_node.get(node_id, [])
        failed_callbacks, errors, _ = self._run_callbacks(callbacks)
        self._record_node_cleanup_result(
            node_id,
            failed_callbacks,
            errors,
            retain_failed_callbacks,
        )
        return errors

    def _record_node_cleanup_result(
        self,
        node_id: str,
        failed_callbacks: tuple[Callable[[], None], ...],
        errors: tuple[Exception, ...],
        retain_failed_callbacks: bool,
    ) -> None:
        if errors:
            if retain_failed_callbacks:
                self.cleanup_by_node[node_id] = list(failed_callbacks)
            else:
                self.cleanup_by_node.pop(node_id, None)
                self.roots_by_node.pop(node_id, None)
            return
        self.cleanup_by_node.pop(node_id, None)
        self.roots_by_node.pop(node_id, None)

    def cleanup_all(self) -> GeneratedFileWorkspaceCleanupResult:
        errors: list[Exception] = []
        cleaned_node_ids: list[str] = []
        for node_id in tuple(self.cleanup_by_node):
            callbacks = self.cleanup_by_node.get(node_id, [])
            failed_callbacks, node_errors, successful_callback_count = (
                self._run_callbacks(callbacks)
            )
            errors.extend(node_errors)
            if successful_callback_count:
                cleaned_node_ids.append(node_id)
            if failed_callbacks:
                self.cleanup_by_node[node_id] = list(failed_callbacks)
                continue
            self.cleanup_by_node.pop(node_id, None)
            self.roots_by_node.pop(node_id, None)
        return GeneratedFileWorkspaceCleanupResult(
            errors=tuple(errors),
            cleaned_node_ids=tuple(sorted(cleaned_node_ids)),
        )

    def cleanup_all_best_effort(self) -> tuple[Exception, ...]:
        return self.cleanup_all().errors

    @staticmethod
    def _run_callbacks(
        callbacks: list[Callable[[], None]],
    ) -> tuple[tuple[Callable[[], None], ...], tuple[Exception, ...], int]:
        failed_callbacks: list[Callable[[], None]] = []
        errors: list[Exception] = []
        successful_callback_count = 0
        for cleanup in callbacks:
            try:
                cleanup()
            except Exception as exc:
                failed_callbacks.append(cleanup)
                errors.append(exc)
            else:
                successful_callback_count += 1
        return tuple(failed_callbacks), tuple(errors), successful_callback_count
