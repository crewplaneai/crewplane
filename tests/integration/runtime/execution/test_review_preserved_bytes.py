import asyncio
import hashlib
from pathlib import Path

import pytest

from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.runtime.execution.review_loop.checkpoint_progress import ProgressRestorer
from crewplane.runtime.execution.sequential import execute_sequential_stage
from tests.helpers.review_checkpoints import (
    checkpoint_run,
    hydrate_checkpoint,
    open_checkpoint,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    review_output,
)


@pytest.mark.parametrize("invalid_role", ["executor", "reviewer"])
def test_review_rounds_and_checkpoint_readback_render_preserved_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_role: str
) -> None:
    async def run() -> None:
        output, runtime = checkpoint_run(tmp_path, audit_rounds=1)
        candidate = b"candidate\r\n" + (
            b"\xff\x1a" if invalid_role == "executor" else b"text"
        )
        reviewer = review_output(verdict="NO_FINDINGS").encode()
        if invalid_role == "reviewer":
            reviewer += b"\r\n\xff\x1a"

        class ByteOutputInvoker(MockAgentInvoker):
            async def invoke(self, *args, **kwargs) -> None:
                await super().invoke(*args, **kwargs)
                payload = (
                    candidate if self.calls[-1]["role"] == "executor" else reviewer
                )
                args[3].write_bytes(payload)

        invoker = ByteOutputInvoker(["candidate", review_output(verdict="NO_FINDINGS")])
        publish = output.write_review_checkpoint

        def interrupt_after_finalization_checkpoint(record):
            path = publish(record)
            if (
                isinstance(record, OpenReviewCheckpoint)
                and record.next_phase == "finalize"
            ):
                raise RuntimeError("interrupted before finalization")
            return path

        monkeypatch.setattr(
            output, "write_review_checkpoint", interrupt_after_finalization_checkpoint
        )
        with pytest.raises(RuntimeError, match="interrupted before finalization"):
            await execute_sequential_stage(
                runtime.plan.nodes[0], output, runtime, invoker
            )

        marker = open_checkpoint(output)
        saved_candidate = marker.progress.latest_executor_outputs[0]
        candidate_path = output.stages_dir / saved_candidate.output_path
        assert candidate_path.read_bytes() == candidate
        saved_review = marker.progress.latest_reviewer_outputs[0]
        assert saved_review.evaluation.raw_text == reviewer.decode(
            "utf-8", errors="replace"
        )
        assert saved_review.evaluation.verdict == "NO_FINDINGS"

        progress = ProgressRestorer(
            marker, output.stages_dir, runtime.plan.nodes[0].provider_records, 1
        ).restore()
        restored_candidate = progress.latest_executor_outputs[0]
        assert restored_candidate.content == candidate.decode("utf-8", errors="replace")
        assert restored_candidate.output_signature == (
            len(candidate),
            hashlib.sha256(candidate).hexdigest(),
        )

        fresh, restored = hydrate_checkpoint(output, runtime)
        after = MockAgentInvoker([])
        await execute_sequential_stage(restored.plan.nodes[0], fresh, restored, after)
        assert not after.calls
        assert (
            fresh.stages_dir / saved_candidate.output_path
        ).read_bytes() == candidate
        assert open_checkpoint(fresh).progress == marker.progress

    asyncio.run(run())


def test_later_audit_seeds_original_executor_bytes(tmp_path: Path) -> None:
    async def run() -> None:
        output, runtime = checkpoint_run(tmp_path, audit_rounds=2)
        candidate = b"candidate\r\n\xff\x1a"

        class ByteOutputInvoker(MockAgentInvoker):
            async def invoke(self, *args, **kwargs) -> None:
                await super().invoke(*args, **kwargs)
                call = self.calls[-1]
                if call["role"] == "executor" and call["round_num"] == 2:
                    args[3].write_bytes(candidate)

        invoker = ByteOutputInvoker(
            [
                "initial candidate",
                review_output(
                    major="- Fix missing coverage", verdict="CHANGES_REQUESTED"
                ),
                "revised candidate",
                review_output(verdict="NO_FINDINGS"),
                review_output(verdict="NO_FINDINGS"),
            ]
        )
        await execute_sequential_stage(runtime.plan.nodes[0], output, runtime, invoker)

        assert len(invoker.calls) == 5
        assert invoker.calls[-1]["audit_round_num"] == 2
        assert invoker.calls[-1]["role"] == "reviewer"
        assert (
            candidate.decode("utf-8", errors="replace") in invoker.calls[-1]["prompt"]
        )
        marker = open_checkpoint(output)
        progress = ProgressRestorer(
            marker, output.stages_dir, runtime.plan.nodes[0].provider_records, 2
        ).restore()
        artifact = progress.latest_executor_outputs[0]
        assert artifact.audit_round_num == 2
        assert artifact.output_file.read_bytes() == candidate
        assert artifact.output_signature == (
            len(candidate),
            hashlib.sha256(candidate).hexdigest(),
        )
        original = (
            artifact.output_file.parent.parent
            / "review-audit-round-1"
            / "exec_executor_0_round2.md"
        )
        assert original.read_bytes() == candidate

    asyncio.run(run())
