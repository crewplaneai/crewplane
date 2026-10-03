from __future__ import annotations

import asyncio
import io
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from crewplane.artifacts.naming import review_checkpoint_relative_path
from crewplane.artifacts.workspace import checkpoint_state
from crewplane.artifacts.workspace.checkpoint_state import PreparedCheckpointWorkspaces
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.core.review_checkpoint_state import CheckpointPhase
from crewplane.runtime.execution.review_loop import checkpoint, orchestration
from crewplane.runtime.execution.review_loop.types import (
    ReviewLoopProgress,
    ReviewLoopRunContext,
)
from crewplane.runtime.execution.sequential import execute_sequential_stage
from tests.helpers.review_checkpoints import checkpoint_run
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    review_output,
)


@pytest.mark.parametrize("missing_recovery", [False, True])
def test_repeated_checkpoint_reuses_dependency_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_recovery: bool
) -> None:
    real_temporary_file = tempfile.TemporaryFile
    recovery_files: list[BinaryIO] = []

    def record_temporary_file(mode: str = "w+b") -> BinaryIO:
        recovery_file = cast(BinaryIO, real_temporary_file(mode=mode))
        recovery_files.append(recovery_file)
        return recovery_file

    monkeypatch.setattr(tempfile, "TemporaryFile", record_temporary_file)

    async def run() -> None:
        async with _completed_review(tmp_path, monkeypatch) as (context, progress):
            output = context.output
            marker = output.read_review_checkpoint("review")
            assert isinstance(marker, OpenReviewCheckpoint)
            marker_path = output.stages_dir / review_checkpoint_relative_path("review")
            registry = context.runtime_context.runtime_publications
            missing_bytes = 0
            if missing_recovery:
                descriptor = marker.files[0]
                registry.publish(
                    output.stages_dir / descriptor.relative_path, descriptor.signature
                )
                missing_bytes = descriptor.signature[0]
            assert len(recovery_files) == 1
            spool = recovery_files[0]
            original_size = os.fstat(spool.fileno()).st_size

            await checkpoint.commit_checkpoint(
                context, progress, "finalize", progress.last_round_num
            )

            assert (
                os.fstat(spool.fileno()).st_size - original_size
                <= marker_path.stat().st_size + missing_bytes
            )
            for descriptor in marker.files:
                path = output.stages_dir / descriptor.relative_path
                recovered = io.BytesIO()
                assert registry.copy_recovery_payload_to(path, recovered)
                assert recovered.getvalue() == path.read_bytes()

    asyncio.run(run())


def test_reused_dependency_mutation_still_rejects_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        async with _completed_review(tmp_path, monkeypatch) as (context, progress):
            output = context.output
            marker = output.read_review_checkpoint("review")
            assert isinstance(marker, OpenReviewCheckpoint)
            marker_path = output.stages_dir / review_checkpoint_relative_path("review")
            original_marker = marker_path.read_bytes()
            candidate = marker.progress.latest_executor_outputs[0]
            dependency = output.stages_dir / candidate.output_path
            original_content = dependency.read_bytes()
            publish = checkpoint_state.publish_checkpoint_workspaces

            def publish_then_mutate(
                root: Path, prepared: PreparedCheckpointWorkspaces
            ) -> None:
                publish(root, prepared)
                dependency.write_bytes(b"changed after preparation")

            monkeypatch.setattr(
                checkpoint_state, "publish_checkpoint_workspaces", publish_then_mutate
            )
            with pytest.raises(
                ValueError,
                match="Checkpoint dependency changed|recovery source does not match",
            ):
                await checkpoint.commit_checkpoint(
                    context, progress, "finalize", progress.last_round_num
                )

            assert marker_path.read_bytes() == original_marker
            recovered = io.BytesIO()
            registry = context.runtime_context.runtime_publications
            assert registry.copy_recovery_payload_to(dependency, recovered)
            assert recovered.getvalue() == original_content

    asyncio.run(run())


@asynccontextmanager
async def _completed_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[ReviewLoopRunContext, ReviewLoopProgress]]:
    output, runtime = checkpoint_run(tmp_path)
    original = orchestration.commit_checkpoint
    captured: list[tuple[ReviewLoopRunContext, ReviewLoopProgress]] = []

    async def record(
        context: ReviewLoopRunContext,
        progress: ReviewLoopProgress,
        phase: CheckpointPhase,
        local_round: int,
    ) -> None:
        await original(context, progress, phase, local_round)
        if phase == "finalize":
            captured.append((context, progress))

    monkeypatch.setattr(orchestration, "commit_checkpoint", record)
    with closing(runtime.runtime_publications):
        await execute_sequential_stage(
            runtime.plan.nodes[0],
            output,
            runtime,
            MockAgentInvoker(
                ["candidate " * 400, review_output(verdict="NO_FINDINGS")]
            ),
        )
        assert len(captured) == 1
        yield captured[0]
