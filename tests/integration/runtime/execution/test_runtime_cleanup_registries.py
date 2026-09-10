import asyncio
from pathlib import Path

import pytest

from crewplane.runtime.execution.runtime_context import (
    DeferredAsyncCleanupRegistry,
    GeneratedFileWorkspaceRegistry,
)


def test_deferred_cleanup_registry_drains_follow_up_tasks() -> None:
    async def run_test() -> tuple[tuple[Exception, ...], list[str], int]:
        registry = DeferredAsyncCleanupRegistry()
        completed: list[str] = []

        async def follow_up_cleanup() -> None:
            completed.append("follow-up")

        async def initial_cleanup() -> None:
            completed.append("initial")
            registry.register(follow_up_cleanup())

        registry.register(initial_cleanup())
        errors = await registry.drain(1.0)
        return errors, completed, len(registry.tasks)

    errors, completed, task_count = asyncio.run(run_test())

    assert errors == ()
    assert completed == ["initial", "follow-up"]
    assert task_count == 0


def test_deferred_cleanup_registry_cancels_follow_up_tasks_after_deadline() -> None:
    async def run_test() -> tuple[
        tuple[Exception, ...],
        tuple[Exception, ...],
        int,
        bool,
        bool,
    ]:
        registry = DeferredAsyncCleanupRegistry()
        release = asyncio.Event()
        completed = False
        cancelled = False

        async def slow_follow_up_cleanup() -> None:
            nonlocal cancelled, completed
            try:
                await release.wait()
                completed = True
            except asyncio.CancelledError:
                cancelled = True
                raise

        async def initial_cleanup() -> None:
            registry.register(slow_follow_up_cleanup())

        registry.register(initial_cleanup())
        timeout_errors = await registry.drain(0.01)
        remaining_task_count = len(registry.tasks)
        follow_up_errors = await registry.drain(1.0)
        return (
            timeout_errors,
            follow_up_errors,
            remaining_task_count,
            completed,
            cancelled,
        )

    timeout_errors, follow_up_errors, remaining_task_count, completed, cancelled = (
        asyncio.run(run_test())
    )

    assert any(isinstance(error, TimeoutError) for error in timeout_errors)
    assert follow_up_errors == ()
    assert remaining_task_count == 0
    assert completed is False
    assert cancelled is True


def test_generated_file_workspace_registry_cleans_pending_callbacks_best_effort(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()
    cleaned: list[str] = []

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    registry.record(
        "node.a",
        tmp_path / "node.a.md",
        tmp_path / "workspace-a",
        lambda: cleaned.append("node.a"),
    )
    registry.record(
        "node.b",
        tmp_path / "node.b.md",
        tmp_path / "workspace-b",
        fail_cleanup,
    )

    errors = registry.cleanup_all_best_effort()

    assert cleaned == ["node.a"]
    assert len(errors) == 1
    assert registry.roots_for_node("node.a") == {}
    assert registry.roots_for_node("node.b") == {
        (tmp_path / "node.b.md").resolve(): (tmp_path / "workspace-b").resolve()
    }


def test_generated_file_workspace_registry_keeps_failed_cleanup_for_retry(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()
    attempts = 0
    cleaned: list[str] = []

    def flaky_cleanup() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary cleanup failure")
        cleaned.append("retry")

    registry.record(
        "node",
        tmp_path / "node.md",
        None,
        flaky_cleanup,
    )

    with pytest.raises(RuntimeError, match="Generated-file workspace cleanup failed"):
        registry.cleanup_node("node")

    assert registry.roots_for_node("node") == {}
    errors = registry.cleanup_all_best_effort()
    assert errors == ()
    assert cleaned == ["retry"]


def test_generated_file_workspace_registry_reports_cleaned_nodes(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()
    cleaned: list[str] = []

    registry.record(
        "node",
        tmp_path / "node.md",
        tmp_path / "workspace",
        lambda: cleaned.append("node"),
    )

    result = registry.cleanup_all()

    assert result.errors == ()
    assert result.cleaned_node_ids == ("node",)
    assert cleaned == ["node"]
    assert registry.roots_for_node("node") == {}


def test_generated_file_workspace_registry_reports_partially_cleaned_nodes(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()
    cleaned: list[str] = []

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    registry.record(
        "node",
        tmp_path / "first.md",
        tmp_path / "workspace-a",
        lambda: cleaned.append("first"),
    )
    registry.record(
        "node",
        tmp_path / "second.md",
        tmp_path / "workspace-b",
        fail_cleanup,
    )

    result = registry.cleanup_all()

    assert len(result.errors) == 1
    assert result.cleaned_node_ids == ("node",)
    assert cleaned == ["first"]
    assert registry.roots_for_node("node") == {
        (tmp_path / "first.md").resolve(): (tmp_path / "workspace-a").resolve(),
        (tmp_path / "second.md").resolve(): (tmp_path / "workspace-b").resolve(),
    }


def test_generated_file_workspace_registry_node_cleanup_can_be_best_effort(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    registry.record(
        "node",
        tmp_path / "node.md",
        tmp_path / "workspace",
        fail_cleanup,
    )

    errors = registry.cleanup_node_best_effort("node")

    assert len(errors) == 1
    assert registry.roots_for_node("node") == {
        (tmp_path / "node.md").resolve(): (tmp_path / "workspace").resolve()
    }


def test_generated_file_workspace_registry_node_cleanup_can_retain_workspace(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    registry.record(
        "node",
        tmp_path / "node.md",
        tmp_path / "workspace",
        fail_cleanup,
    )

    errors = registry.cleanup_node_best_effort(
        "node",
        retain_failed_callbacks=False,
    )

    assert len(errors) == 1
    assert registry.roots_for_node("node") == {}
    assert registry.cleanup_all_best_effort() == ()
