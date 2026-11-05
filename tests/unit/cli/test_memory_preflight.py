import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from crewplane.artifacts.memory.store import MemoryStore
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.cli.run.context import build_workflow_run_context
from crewplane.cli.run.preflight import compile_preview_async, compile_workflow_preview
from crewplane.core.memory.models import MemorySnapshot
from crewplane.core.preflight.source import (
    PreflightWorkflowSource,
    load_workflow_source_for_preflight,
)
from tests.helpers.foreach import (
    copy_foreach_fixture,
    workflow_document,
    write_workflow_document,
)
from tests.helpers.project_memory import memory_config, memory_workflow


def test_disabled_compilation_never_opens_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = memory_workflow("do work")
    task.memory = False
    config = memory_config()
    config.settings.memory = config.settings.memory.model_copy(update={"learn": True})
    snapshot = AsyncMock(side_effect=AssertionError("must not inspect memory"))
    monkeypatch.setattr(MemoryStore, "snapshot", snapshot)
    preview = compile_workflow_preview(
        config,
        PreflightWorkflowSource.from_workflow(task),
        Console(),
        True,
        "read_only",
        tmp_path,
    )
    assert not preview.has_errors(), preview.diagnostics
    assert preview.memory_selections == []
    snapshot.assert_not_awaited()
    assert not (tmp_path / ".crewplane").exists()


@pytest.mark.parametrize("root_enabled", [False, True])
def test_single_snapshot_covers_enabled_foreach_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_enabled: bool
) -> None:
    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    payload["memory"] = root_enabled
    write_workflow_document(path, payload, body)
    child = tmp_path / "child.md"
    payload, body = workflow_document(child)
    payload["memory"] = True
    write_workflow_document(child, payload, body)
    source = load_workflow_source_for_preflight(path, tmp_path)
    snapshot = AsyncMock(return_value=MemorySnapshot())
    monkeypatch.setattr(MemoryStore, "snapshot", snapshot)
    preview = compile_workflow_preview(
        memory_config(),
        source,
        Console(),
        True,
        "read_only",
        tmp_path,
    )
    assert not preview.has_errors(), preview.diagnostics
    snapshot.assert_awaited_once()
    assert preview.foreach_templates[0].memory_selections
    assert bool(preview.memory_selections) is root_enabled
    assert not (tmp_path / ".crewplane").exists()


@pytest.mark.parametrize("extractor", [None, "absent", "alpha"])
def test_extractor_policy_is_required_only_for_enabled_learning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extractor: str | None
) -> None:
    config = memory_config()
    config.settings.memory = config.settings.memory.model_copy(
        update={"learn": True, "extractor": extractor}
    )
    snapshot = AsyncMock(return_value=MemorySnapshot())
    monkeypatch.setattr(MemoryStore, "snapshot", snapshot)
    preview = compile_workflow_preview(
        config,
        PreflightWorkflowSource.from_workflow(memory_workflow("do work")),
        Console(),
        True,
        "read_only",
        tmp_path,
    )
    assert preview.has_errors() is (extractor != "alpha")
    if extractor != "alpha":
        snapshot.assert_not_awaited()
        assert any("extractor" in item.message for item in preview.diagnostics)
    else:
        snapshot.assert_awaited_once()


def test_async_compilation_cancels_pending_snapshot_without_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        waiting, cancelled = asyncio.Event(), asyncio.Event()

        async def wait_for_snapshot(store: MemoryStore) -> MemorySnapshot:
            del store
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

        monkeypatch.setattr(MemoryStore, "snapshot", wait_for_snapshot)
        config = memory_config()
        console = Console()
        context = build_workflow_run_context(
            config,
            PreflightWorkflowSource.from_workflow(memory_workflow("do work")),
            console,
            tmp_path,
        )
        runtime = build_runtime_config_snapshot(config, console, no_live=True)
        compilation = asyncio.create_task(
            compile_preview_async(context, runtime, "read_only")
        )
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            compilation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(compilation, 2)
            assert cancelled.is_set()
            assert not (tmp_path / ".crewplane").exists()
        finally:
            compilation.cancel()
            await asyncio.gather(compilation, return_exceptions=True)

    asyncio.run(scenario())
