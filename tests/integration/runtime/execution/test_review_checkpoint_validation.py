from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.artifacts.resume import checkpoint_validation
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.runtime.execution.sequential import execute_sequential_stage
from tests.helpers.review_checkpoints import checkpoint_run, open_checkpoint
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    provider_failure,
    review_output,
)


@pytest.fixture(params=[1, 2])
def evidence_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[Path, PreflightExecutionPlan, OpenReviewCheckpoint]:
    output, runtime = checkpoint_run(
        tmp_path,
        audit_rounds=request.param,
        continue_on_failure=True,
        providers=[
            {"provider": "exec", "role": "executor"},
            {"provider": "review", "role": "reviewer"},
            {"provider": "review", "role": "reviewer"},
        ],
    )
    publish = output.write_review_checkpoint

    def interrupt(record):
        path = publish(record)
        if (
            isinstance(record, OpenReviewCheckpoint)
            and record.next_phase == "executors"
        ):
            raise RuntimeError("settled reviewer batch")
        return path

    class OneFailedReviewer(MockAgentInvoker):
        async def invoke(self, *args, **kwargs):
            await super().invoke(*args, **kwargs)
            if kwargs["invocation_context"].task_id == "review_reviewer_1":
                raise provider_failure("settled reviewer failure")

    monkeypatch.setattr(output, "write_review_checkpoint", interrupt)
    with pytest.raises(RuntimeError, match="settled reviewer batch"):
        asyncio.run(
            execute_sequential_stage(
                runtime.plan.nodes[0],
                output,
                runtime,
                OneFailedReviewer(
                    [
                        "candidate",
                        review_output(verdict="NO_FINDINGS"),
                        "partial review",
                    ]
                ),
            )
        )
    checkpoint = open_checkpoint(output)
    assert len(checkpoint.progress.candidates()) == 3
    assert len(checkpoint.progress.reviews()) == 1
    assert len(checkpoint.progress.failures()) == 1
    return output.stages_dir, runtime.plan, checkpoint


@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "descriptor",
        "candidate_identity",
        "normalized_review",
        "raw_review",
        "review_state",
        "failure_state",
        "candidate_json",
        "state_json",
        "state_shape",
        "review_encoding",
    ],
)
def test_checkpoint_evidence_preserves_reads_errors_and_inputs(
    evidence_checkpoint: tuple[Path, PreflightExecutionPlan, OpenReviewCheckpoint],
    damage: str,
) -> None:
    root, plan, checkpoint = evidence_checkpoint
    candidate = checkpoint.progress.candidates()[0]
    review = checkpoint.progress.reviews()[0]
    identity_path = (
        Path(candidate.output_path).with_suffix(".candidate.json").as_posix()
    )
    raw_path = Path(review.output_path).with_suffix(".raw.txt").as_posix()
    state_path = next(
        item.relative_path
        for item in checkpoint.files
        if item.purpose == "review_state"
    )
    failure_path = next(
        item.relative_path
        for item in checkpoint.files
        if item.purpose == "review_failure"
    )
    paths = [
        identity_path,
        identity_path,
        identity_path,
        review.output_path,
        raw_path,
        state_path,
        failure_path,
    ]
    errors = {
        "descriptor": (0, "Checkpoint review evidence lacks matching descriptors."),
        "candidate_identity": (
            1,
            "Checkpoint candidate identity disagrees with its evidence.",
        ),
        "normalized_review": (
            4,
            "Checkpoint evaluation disagrees with its published review.",
        ),
        "raw_review": (5, "Checkpoint evaluation disagrees with its original review."),
        "review_state": (
            6,
            "Checkpoint review state disagrees with its evaluation or failure.",
        ),
        "failure_state": (
            7,
            "Checkpoint review state disagrees with its evaluation or failure.",
        ),
        "state_shape": (
            6,
            "Checkpoint review state disagrees with its evaluation or failure.",
        ),
    }
    if damage == "descriptor":
        checkpoint.files = [
            item.model_copy(update={"purpose": "review_metadata"})
            if item.relative_path == identity_path
            else item
            for item in checkpoint.files
        ]
    elif damage != "none":
        damaged_path = {
            "candidate_identity": identity_path,
            "candidate_json": identity_path,
            "normalized_review": review.output_path,
            "raw_review": raw_path,
            "review_encoding": review.output_path,
            "review_state": state_path,
            "state_json": state_path,
            "state_shape": state_path,
            "failure_state": failure_path,
        }[damage]
        path = root / damaged_path
        if damage in {"candidate_json", "state_json"}:
            path.write_bytes(b"{")
        elif damage == "state_shape":
            path.write_bytes(b"[]")
        elif damage == "review_encoding":
            path.write_bytes(b"\xff")
        elif damage in {"normalized_review", "raw_review"}:
            path.write_text("different review", encoding="utf-8")
        else:
            payload = json.loads(path.read_bytes())
            field = "reason" if damage == "candidate_identity" else "warnings"
            payload[field] = "different evidence"
            path.write_text(json.dumps(payload), encoding="utf-8")
        checkpoint.files = [
            item.model_copy(update={"signature": file_size_and_sha256(path)})
            if item.relative_path == damaged_path
            else item
            for item in checkpoint.files
        ]

    before = checkpoint.model_dump()
    contents = {
        item.relative_path: (root / item.relative_path).read_bytes()
        for item in checkpoint.files
    }
    with patch.object(
        checkpoint_validation,
        "read_checkpoint_file",
        wraps=checkpoint_validation.read_checkpoint_file,
    ) as read:
        if damage == "none":
            assert (
                checkpoint_validation.require_checkpoint_dependencies(
                    root, plan, plan.nodes[0], checkpoint
                )
                is None
            )
            count = len(paths)
        else:
            exception = ValueError
            if damage in {"candidate_json", "state_json"}:
                exception = json.JSONDecodeError
                count = 1 if damage == "candidate_json" else 6
                message = "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
            elif damage == "review_encoding":
                exception = UnicodeDecodeError
                count = 4
                message = "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
            else:
                count, message = errors[damage]
            with pytest.raises(exception) as caught:
                checkpoint_validation.require_checkpoint_dependencies(
                    root, plan, plan.nodes[0], checkpoint
                )
            assert type(caught.value) is exception
            assert str(caught.value) == message
    assert [call.args[1].relative_path for call in read.call_args_list] == paths[:count]
    assert checkpoint.model_dump() == before
    assert {
        relative: (root / relative).read_bytes() for relative in contents
    } == contents
