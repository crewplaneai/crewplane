import asyncio
import json
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event, get_ident

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.results.findings import FindingsExtractionError
from crewplane.artifacts.results.review_loop_status import ReviewLoopStopReason
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.fragment_assembler import ResolvedPrompt
from crewplane.runtime.execution.review_loop import (
    completion_policy,
    initial_candidate,
    orchestration,
)
from crewplane.runtime.execution.review_loop.candidate_identity import CandidateIdentity
from crewplane.runtime.execution.review_loop.types import (
    AuditRoundResult,
    ExecutorRoundArtifact,
    ExecutorRoundRunResult,
    ReviewerRoundRunResult,
    ReviewLoopProgress,
    ReviewLoopRunContext,
)
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.review_checkpoints import checkpoint_payload
from tests.integration.runtime.execution.review_loop_rounds_support import (
    make_review_node,
    make_round_runtime_context,
)


@pytest.fixture
def context(tmp_path: Path) -> Iterator[ReviewLoopRunContext]:
    node = make_review_node()
    output = OutputManager("workflow", base_dir=tmp_path)
    executor, reviewer = node.provider_records
    run_context = ReviewLoopRunContext(
        runtime_context=make_round_runtime_context(),
        stage=node,
        output=output,
        node_dir=output.create_node_dir(node_artifact_request(node.id)),
        invoker=object(),
        telemetry=None,
        executors=(executor,),
        reviewers=(reviewer,),
        executor_prompt="Implement.",
        reviewer_prompt_context="Review.",
        remediation_depth=1,
        audit_rounds=1,
    )
    try:
        yield run_context
    finally:
        run_context.runtime_context.runtime_publications.close()


def candidate(context: ReviewLoopRunContext) -> ExecutorRoundArtifact:
    provider = context.executors[0]
    path = context.node_dir / "candidate.md"
    path.write_text("Canonical candidate.", encoding="utf-8")
    return ExecutorRoundArtifact(
        provider,
        provider.task_id,
        path.read_text(),
        path,
        1,
        2,
        output_signature=file_size_and_sha256(path),
        candidate_identity=CandidateIdentity("document", "fingerprint"),
    )


@pytest.mark.parametrize("outcome", ["consensus", "missing", "fatal", "continue"])
def test_audit_terminal_publication_precedes_logging_and_policy_error(
    context: ReviewLoopRunContext, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    context.stage = context.stage.model_copy(
        update={
            "execution_policy": context.stage.execution_policy.model_copy(
                update={
                    "consensus_on_exhaustion": "continue"
                    if outcome == "continue"
                    else "fatal"
                }
            )
        }
    )
    progress = ReviewLoopProgress()
    outputs = None if outcome == "missing" else [candidate(context)]
    events: list[str] = []
    persist = orchestration.persist_review_loop_status

    async def execute(request) -> ExecutorRoundRunResult:
        assert request.node is context.stage
        return ExecutorRoundRunResult([candidate(context)], 0)

    async def audit(request) -> AuditRoundResult:
        assert request.stage is context.stage and request.audit_round_num is None
        return AuditRoundResult(
            outcome == "consensus", outcome == "consensus", outputs, [], 0, 0, 0, 1
        )

    def publish(node_dir, payload) -> Path:
        path = persist(node_dir, payload)
        assert json.loads(path.read_text()) == payload
        events.append("status")
        return path

    def log(*args, **kwargs) -> None:
        del args
        events.append(kwargs["operation"])

    monkeypatch.setattr(initial_candidate, "run_executor_round", execute)
    monkeypatch.setattr(orchestration, "execute_single_audit_round", audit)
    monkeypatch.setattr(orchestration, "persist_review_loop_status", publish)
    monkeypatch.setattr(completion_policy, "emit_runtime_log", log)
    if outcome in {"missing", "fatal"}:
        with pytest.raises(orchestration.ReviewPolicyError):
            asyncio.run(orchestration.execute_review_loop_audits(context, progress))
    else:
        assert (
            asyncio.run(orchestration.execute_review_loop_audits(context, progress))
            is None
        )

    assert progress.cursor_round == 1 and progress.next_phase == "executors"
    assert progress.executed_audit_rounds == 1
    assert progress.consensus_reached is (outcome == "consensus")
    assert progress.continued_after_exhaustion is (outcome == "continue")
    operation = (
        "review_loop_no_canonical_candidate"
        if outcome == "missing"
        else "review_loop_consensus_exhausted"
    )
    assert events == (
        ["status", "status"]
        if outcome == "consensus"
        else ["status", "status", operation]
    )
    path = context.node_dir / "review-state" / "review-loop-status.json"
    assert context.runtime_context.runtime_publications.snapshot()[0][path] == (
        file_size_and_sha256(path)
    )


@pytest.mark.parametrize("continued", [False, True])
def test_stall_public_contract_publishes_before_policy_error(
    context: ReviewLoopRunContext, monkeypatch: pytest.MonkeyPatch, continued: bool
) -> None:
    context.stage = context.stage.model_copy(
        update={
            "execution_policy": context.stage.execution_policy.model_copy(
                update={"continue_on_failure": continued}
            )
        }
    )
    progress = ReviewLoopProgress(stop_reason=ReviewLoopStopReason.NO_PROGRESS)
    persisted: list[bool] = []
    persist = orchestration.persist_review_loop_status

    def publish(node_dir, payload) -> Path:
        persisted.append(payload["continued_after_stop"])
        return persist(node_dir, payload)

    monkeypatch.setattr(orchestration, "persist_review_loop_status", publish)
    if continued:
        assert orchestration.finish_stalled_review_loop(context, progress) is None
    else:
        with pytest.raises(orchestration.ReviewPolicyError, match="no_progress"):
            orchestration.finish_stalled_review_loop(context, progress)
    assert persisted == [continued]
    assert progress.continued_after_stop is continued


@pytest.mark.parametrize("signature_state", ["valid", "missing", "mismatch"])
def test_seed_preserves_identity_provenance_and_publication_requirements(
    context: ReviewLoopRunContext, signature_state: str
) -> None:
    artifact = candidate(context)
    if signature_state == "missing":
        artifact = replace(artifact, output_signature=None)
    elif signature_state == "mismatch":
        artifact = replace(artifact, content="Unpublished replacement.")
    audit_dir = context.node_dir / "audit-round-2"
    audit_dir.mkdir()
    if signature_state != "valid":
        with pytest.raises(RuntimeError):
            orchestration.seed_executor_outputs(
                context.runtime_context, context.stage.id, audit_dir, [artifact], 2, 1
            )
        assert list(audit_dir.iterdir()) == []
        return

    seeded = orchestration.seed_executor_outputs(
        context.runtime_context, context.stage.id, audit_dir, [artifact], 2, 1
    )
    result = seeded[0]
    assert result.content == artifact.content == result.output_file.read_text()
    assert result.output_signature == artifact.output_signature
    assert result.candidate_identity is artifact.candidate_identity
    assert (result.audit_round_num, result.round_num) == (2, 1)
    assert (result.producer_audit, result.producer_round) == (1, 2)
    assert json.loads(
        result.output_file.with_suffix(".candidate.json").read_text()
    ) == (asdict(artifact.candidate_identity))
    assert context.runtime_context.runtime_publications.snapshot()[0][
        result.output_file
    ] == (artifact.output_signature)


@pytest.mark.parametrize(
    "boundary",
    [
        "seed",
        "status",
        "stall_status",
        "exhausted_status",
        "round_status",
        "consensus_round_status",
        "intermediate_status",
    ],
)
@pytest.mark.parametrize("storage_failure", [False, True])
def test_initial_candidate_and_terminal_status_io_drain_before_owner_cleanup(
    context: ReviewLoopRunContext,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    storage_failure: bool,
) -> None:
    progress = ReviewLoopProgress(latest_executor_outputs=[candidate(context)])
    round_publication = boundary in {
        "round_status",
        "consensus_round_status",
        "intermediate_status",
    }
    if round_publication:
        progress.cursor_round = 2
        progress.next_phase = "reviewers"
    if boundary == "intermediate_status":
        context.audit_rounds = 2
    audit_dir = context.node_dir / "audit-round-2"
    audit_dir.mkdir()
    release = Event()
    completed = Event()
    failure = OSError("publication failed")
    cleanup: list[bool] = []
    logged_operations: list[str] = []
    owner_context = ContextVar("orchestration-test-owner", default="missing")
    original = (
        initial_candidate.seed_executor_outputs
        if boundary == "seed"
        else orchestration.persist_review_loop_status
    )

    context.stage = context.stage.model_copy(
        update={
            "execution_policy": context.stage.execution_policy.model_copy(
                update={
                    "continue_on_failure": boundary == "stall_status",
                    "consensus_on_exhaustion": "continue",
                }
            )
        }
    )

    async def audit(request) -> AuditRoundResult:
        assert request.stage is context.stage
        assert request.audit_round_num == (1 if context.audit_rounds > 1 else None)
        stalled = boundary == "stall_status"
        consensus = not stalled and boundary not in {
            "exhausted_status",
            "round_status",
            "intermediate_status",
        }
        return AuditRoundResult(
            consensus,
            consensus,
            progress.latest_executor_outputs,
            [],
            0,
            0,
            0,
            1,
            stop_reason=ReviewLoopStopReason.NO_PROGRESS if stalled else None,
        )

    def log(*args, **kwargs) -> None:
        del args
        logged_operations.append(kwargs["operation"])

    monkeypatch.setattr(completion_policy, "emit_runtime_log", log)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        owner_thread = get_ident()

        def publish(*args, **kwargs):
            if boundary != "seed":
                payload = args[1]
                terminal = (
                    payload["continued_after_stop"]
                    if boundary == "stall_status"
                    else payload["stop_reason"]
                    == (
                        "consensus_exhausted"
                        if boundary == "exhausted_status"
                        else "consensus"
                    )
                )
                if not round_publication and not terminal:
                    return original(*args, **kwargs)
            assert get_ident() != owner_thread, "Publication blocked the event loop"
            assert owner_context.get() == "owner context"
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=2)
            result = original(*args, **kwargs)
            completed.set()
            if storage_failure:
                raise failure
            return result

        async def owner() -> None:
            owner_context.set("owner context")
            try:
                if boundary == "seed":
                    await initial_candidate.initial_audit_executor_outputs(
                        context, progress, audit_dir, 2
                    )
                else:
                    await orchestration.execute_review_loop_audits(context, progress)
            finally:
                cleanup.append(completed.is_set())

        if boundary == "seed":
            monkeypatch.setattr(initial_candidate, "seed_executor_outputs", publish)
        else:
            monkeypatch.setattr(orchestration, "persist_review_loop_status", publish)
            monkeypatch.setattr(orchestration, "execute_single_audit_round", audit)
        task = asyncio.create_task(owner())
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel("first cancellation")
            await asyncio.sleep(0)
            task.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            assert cleanup == []
        finally:
            release.set()
            error: BaseException | None = None
            try:
                await task
            except (OSError, asyncio.CancelledError) as exc:
                error = exc
        if storage_failure:
            assert error is failure
        else:
            assert isinstance(error, asyncio.CancelledError)

    asyncio.run(run())
    assert cleanup == [True]
    assert logged_operations == []
    if round_publication:
        assert progress.cursor_round == 2 and progress.next_phase == "reviewers"
        assert progress.stop_reason is None
        assert not progress.continued_after_exhaustion


@pytest.mark.parametrize(
    "outcome",
    [
        "fatal",
        "fatal_round",
        "missing",
        "missing_round",
        "stall_round",
        "stall_terminal",
        "execution",
        "findings",
        "cancel",
    ],
)
@pytest.mark.parametrize("publication_failure", [False, True])
@pytest.mark.parametrize("cancellation", ["uninterrupted", "cancelled"])
def test_settled_failure_survives_status_publication_cancellation(
    context: ReviewLoopRunContext,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    publication_failure: bool,
    cancellation: str,
) -> None:
    context.stage = context.stage.model_copy(
        update={
            "execution_policy": context.stage.execution_policy.model_copy(
                update={
                    "consensus_on_exhaustion": "fatal",
                    "continue_on_failure": False,
                }
            )
        }
    )
    events: list[str] = []
    release = Event()
    completed = Event()
    cleanup: list[bool] = []
    storage_error = OSError("status publication failed")
    original_error = (
        asyncio.CancelledError("original cancellation")
        if outcome == "cancel"
        else FindingsExtractionError("invalid findings")
        if outcome == "findings"
        else NodeExecutionError("execution failed")
    )
    outputs = [candidate(context)]
    rejected = (None, 1, {"exec"}, "rejected")
    rejected_invocations: list[list[tuple[int | None, int, set[str], str]]] = []
    persist = orchestration.persist_review_loop_status
    policy_rejection = outcome.startswith(("fatal", "missing", "stall"))
    publication_index = (
        3
        if outcome == "findings"
        else 1
        if outcome.endswith("_round") or not policy_rejection
        else 2
    )

    def prompt(*args, **kwargs) -> ResolvedPrompt:
        del args, kwargs
        return ResolvedPrompt("Resolved task.")

    async def executor(request) -> ExecutorRoundRunResult:
        assert request.node is context.stage
        return ExecutorRoundRunResult(outputs, 0)

    async def audit(request) -> AuditRoundResult:
        assert request.stage is context.stage
        request.rejected_invocations.append(rejected)
        rejected_invocations.append(request.rejected_invocations)
        if outcome in {"execution", "cancel"}:
            raise original_error
        return AuditRoundResult(
            outcome == "findings",
            outcome == "findings",
            None if outcome.startswith("missing") else outputs,
            [],
            0,
            0,
            0,
            1,
            stop_reason=ReviewLoopStopReason.NO_PROGRESS
            if outcome.startswith("stall")
            else None,
        )

    def findings(*args) -> None:
        del args
        if outcome == "findings":
            raise original_error

    def close(run_context, reason) -> None:
        assert run_context.stage is context.stage and reason
        events.append("close")

    def discard(output, stage, task_ids, audit, local_round, reason) -> None:
        assert output is context.output and stage is context.stage
        assert (audit, local_round, task_ids, reason) == rejected
        events.append("discard")

    def log(*args, **kwargs) -> None:
        del args
        events.append(kwargs["operation"])

    monkeypatch.setattr(
        orchestration, "resolve_prompt_with_output_budget_details", prompt
    )
    monkeypatch.setattr(orchestration, "resolve_reviewer_prompt_context", prompt)
    monkeypatch.setattr(initial_candidate, "run_executor_round", executor)
    monkeypatch.setattr(orchestration, "execute_single_audit_round", audit)
    monkeypatch.setattr(orchestration, "validate_final_findings", findings)
    monkeypatch.setattr(orchestration, "close_checkpoint", close)
    monkeypatch.setattr(orchestration, "discard_executor_workspace_lineage", discard)
    monkeypatch.setattr(completion_policy, "emit_runtime_log", log)

    async def run() -> BaseException:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        publication_count = 0

        def publish(node_dir, payload) -> Path:
            nonlocal publication_count
            publication_count += 1
            if publication_count == publication_index:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(timeout=2)
            path = persist(node_dir, payload)
            events.append(payload["stop_reason"] or "status")
            if publication_count == publication_index:
                completed.set()
                if publication_failure:
                    raise storage_error
            return path

        async def owner() -> None:
            try:
                await orchestration.execute_review_loop_stage(
                    context.stage,
                    context.output,
                    context.node_dir,
                    context.runtime_context,
                    context.invoker,
                    context.telemetry,
                )
            finally:
                cleanup.append(completed.is_set())

        monkeypatch.setattr(orchestration, "persist_review_loop_status", publish)
        task = asyncio.create_task(owner())
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            if cancellation == "cancelled":
                task.cancel("first cancellation")
                await asyncio.sleep(0)
                task.cancel("second cancellation")
                await asyncio.sleep(0)
            assert not task.done()
            assert cleanup == []
        finally:
            release.set()
            try:
                await task
            except BaseException as exc:
                error = exc
            else:
                pytest.fail("The stage must propagate its settled failure")
        return error

    error = asyncio.run(run())
    assert cleanup == [True]
    if publication_failure:
        assert error is storage_error
        assert not any(event.startswith("review_loop_") for event in events)
        assert "discard" not in events
    elif policy_rejection:
        assert isinstance(error, orchestration.ReviewPolicyError)
        reason = (
            "no_progress"
            if outcome.startswith("stall")
            else "no_valid_candidate"
            if outcome.startswith("missing")
            else "consensus_exhausted"
        )
        operation = (
            "review_loop_stopped"
            if outcome.startswith("stall")
            else "review_loop_no_canonical_candidate"
            if outcome.startswith("missing")
            else "review_loop_consensus_exhausted"
        )
        assert events == [
            reason if outcome.startswith("stall") else "status",
            reason,
            operation,
            "close",
            "discard",
        ]
        assert rejected_invocations == [[]]
    elif outcome == "findings":
        assert isinstance(error, NodeExecutionError)
        assert error.__cause__ is original_error
        assert events == ["status", "consensus", "close", "consensus"]
        assert rejected_invocations == [[rejected]]
    else:
        assert error is original_error
        assert events == ["cancelled" if outcome == "cancel" else "failed"]
        assert rejected_invocations == [[rejected]]


@pytest.mark.parametrize(
    "failure_at",
    [
        "restore",
        "prompt",
        "audit",
        "policy",
        "cancel",
        "findings",
        "findings_close",
        "close",
        "discard",
        "commit",
        "none",
    ],
)
def test_stage_preserves_preparation_and_failure_boundaries(
    context: ReviewLoopRunContext,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
) -> None:
    events: list[str] = []
    persist = orchestration.persist_review_loop_status
    failure = {
        "policy": orchestration.ReviewPolicyError("settled policy"),
        "cancel": asyncio.CancelledError("cancelled invocation"),
        "findings": FindingsExtractionError("invalid findings"),
    }.get(failure_at, OSError("invocation failed"))

    def restore(*args) -> ReviewLoopProgress:
        del args
        if failure_at == "restore":
            raise failure
        return ReviewLoopProgress()

    def prompt(*args, **kwargs) -> ResolvedPrompt:
        del args, kwargs
        if failure_at == "prompt":
            raise failure
        return ResolvedPrompt("Resolved task.")

    async def audit(run_context, progress) -> None:
        assert run_context.stage is context.stage
        events.append("audit")
        run_context.rejected_invocations.append((None, 1, {"exec"}, "rejected"))
        if failure_at in {"audit", "policy", "cancel"}:
            raise failure
        if failure_at in {"close", "discard"}:
            raise orchestration.ReviewPolicyError("settled policy")
        progress.last_round_num = 2

    def findings(run_context, progress) -> None:
        del run_context, progress
        events.append("findings")
        if failure_at == "findings":
            raise failure
        if failure_at == "findings_close":
            raise FindingsExtractionError("invalid findings")

    def close(run_context, reason) -> None:
        del run_context
        assert reason == {
            "close": "settled policy",
            "discard": "settled policy",
            "findings_close": "invalid findings",
        }.get(failure_at, str(failure))
        events.append("close")
        if failure_at in {"close", "findings_close"}:
            raise failure

    def discard(*args) -> None:
        del args
        events.append("discard")
        if failure_at == "discard":
            raise failure

    def publish(node_dir, payload) -> Path:
        assert node_dir == context.node_dir
        events.append(payload["stop_reason"])
        return persist(node_dir, payload)

    async def commit(run_context, progress, phase, local_round) -> None:
        del run_context, progress
        assert (phase, local_round) == ("finalize", 2)
        events.append("finalize")
        if failure_at == "commit":
            raise failure

    if failure_at == "restore":
        context.runtime_context.review_checkpoints[context.stage.id] = (
            OpenReviewCheckpoint.model_validate(checkpoint_payload())
        )
        monkeypatch.setattr(orchestration.ProgressRestorer, "restore", restore)
    monkeypatch.setattr(
        orchestration, "resolve_prompt_with_output_budget_details", prompt
    )
    monkeypatch.setattr(orchestration, "resolve_reviewer_prompt_context", prompt)
    monkeypatch.setattr(orchestration, "execute_review_loop_audits", audit)
    monkeypatch.setattr(orchestration, "validate_final_findings", findings)
    monkeypatch.setattr(orchestration, "close_checkpoint", close)
    monkeypatch.setattr(orchestration, "discard_executor_workspace_lineage", discard)
    monkeypatch.setattr(orchestration, "persist_review_loop_status", publish)
    monkeypatch.setattr(orchestration, "commit_checkpoint", commit)
    invocation = orchestration.execute_review_loop_stage(
        context.stage,
        context.output,
        context.node_dir,
        context.runtime_context,
        context.invoker,
        context.telemetry,
    )
    if failure_at == "none":
        assert asyncio.run(invocation) is None
    elif failure_at == "findings":
        with pytest.raises(NodeExecutionError) as caught:
            asyncio.run(invocation)
        assert caught.value.__cause__ is failure
    else:
        with pytest.raises(type(failure)) as caught:
            asyncio.run(invocation)
        assert caught.value is failure
    assert (
        events
        == {
            "restore": [],
            "prompt": [],
            "audit": ["audit", "failed"],
            "policy": ["audit", "close", "discard"],
            "cancel": ["audit", "cancelled"],
            "findings": ["audit", "findings", "close", "failed"],
            "findings_close": ["audit", "findings", "close", "failed"],
            "close": ["audit", "close"],
            "discard": ["audit", "close", "discard"],
            "commit": ["audit", "findings", "finalize", "failed"],
            "none": ["audit", "findings", "finalize", "discard"],
        }[failure_at]
    )


def test_finalized_restore_skips_prompt_resolution_and_execution(
    context: ReviewLoopRunContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    progress = ReviewLoopProgress(next_phase="finalize")

    def restore(*args) -> ReviewLoopProgress:
        del args
        return progress

    def unexpected(*args, **kwargs) -> None:
        del args, kwargs
        pytest.fail("A finalized checkpoint must only restore diagnostics")

    context.runtime_context.review_checkpoints[context.stage.id] = (
        OpenReviewCheckpoint.model_validate(checkpoint_payload())
    )
    monkeypatch.setattr(orchestration.ProgressRestorer, "restore", restore)
    monkeypatch.setattr(
        orchestration, "resolve_prompt_with_output_budget_details", unexpected
    )
    monkeypatch.setattr(orchestration, "execute_review_loop_audits", unexpected)
    assert (
        asyncio.run(
            orchestration.execute_review_loop_stage(
                context.stage,
                context.output,
                context.node_dir,
                context.runtime_context,
                context.invoker,
                context.telemetry,
            )
        )
        is None
    )
    status = context.node_dir / "review-state" / "review-loop-status.json"
    assert json.loads(status.read_text())["stop_reason"] is None


@pytest.mark.parametrize(
    "boundary",
    [
        "restore",
        "executor_prompt",
        "reviewer_prompt",
        "initial_review_prompt",
        "directory",
        "close",
        "findings_close",
        "discard",
        "committed_discard",
        "log",
    ],
)
@pytest.mark.parametrize("storage_failure", [False, True])
def test_stage_io_drains_before_owner_cleanup(
    context: ReviewLoopRunContext,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    storage_failure: bool,
) -> None:
    events: list[str] = []
    release = Event()
    completed = Event()
    cleanup: list[bool] = []
    failure = OSError("storage failed")
    policy_error = orchestration.ReviewPolicyError("settled policy")
    findings_error = FindingsExtractionError("invalid findings")
    owner_context = ContextVar("stage-io-owner", default="missing")
    context.stage = context.stage.model_copy(
        update={
            "execution_policy": context.stage.execution_policy.model_copy(
                update={
                    "consensus_on_exhaustion": "fatal",
                    "audit_rounds": 2 if boundary == "directory" else 1,
                }
            )
        }
    )

    def prompt(*args, **kwargs) -> ResolvedPrompt:
        del args, kwargs
        return ResolvedPrompt("Resolved task.")

    def restore(*args) -> ReviewLoopProgress:
        del args
        return ReviewLoopProgress()

    def close(*args) -> None:
        del args
        events.append("close")

    def discard(*args) -> None:
        del args
        events.append("discard")

    async def commit(*args) -> None:
        del args
        events.append("commit")

    async def audits(run_context, progress) -> None:
        del progress
        if boundary in {"close", "discard", "committed_discard"}:
            run_context.rejected_invocations.append((None, 1, {"exec"}, "rejected"))
        if boundary in {"close", "discard"}:
            raise policy_error

    def findings(*args) -> None:
        del args
        if boundary == "findings_close":
            raise findings_error

    async def executor(*args) -> ExecutorRoundRunResult:
        del args
        return ExecutorRoundRunResult([candidate(context)], 0)

    async def audit(*args) -> AuditRoundResult:
        del args
        consensus = boundary != "log"
        return AuditRoundResult(
            consensus, consensus, [candidate(context)], [], 0, 0, 0, 1
        )

    async def reviewer(*args) -> ReviewerRoundRunResult:
        del args
        events.append("reviewer")
        return ReviewerRoundRunResult([], 0)

    monkeypatch.setattr(
        orchestration, "resolve_prompt_with_output_budget_details", prompt
    )
    monkeypatch.setattr(orchestration, "resolve_reviewer_prompt_context", prompt)
    monkeypatch.setattr(
        initial_candidate, "resolve_prompt_with_output_budget_details", prompt
    )
    monkeypatch.setattr(orchestration, "close_checkpoint", close)
    monkeypatch.setattr(orchestration, "discard_executor_workspace_lineage", discard)
    monkeypatch.setattr(orchestration, "commit_checkpoint", commit)
    monkeypatch.setattr(orchestration, "validate_final_findings", findings)
    monkeypatch.setattr(initial_candidate, "run_executor_round", executor)
    monkeypatch.setattr(initial_candidate, "run_reviewer_round", reviewer)
    monkeypatch.setattr(orchestration, "execute_single_audit_round", audit)
    if boundary == "restore":
        context.runtime_context.review_checkpoints[context.stage.id] = (
            OpenReviewCheckpoint.model_validate(checkpoint_payload())
        )
        monkeypatch.setattr(orchestration.ProgressRestorer, "restore", restore)
    if boundary not in {"directory", "log"}:
        monkeypatch.setattr(orchestration, "execute_review_loop_audits", audits)

    module, name = {
        "restore": (orchestration.ProgressRestorer, "restore"),
        "executor_prompt": (orchestration, "resolve_prompt_with_output_budget_details"),
        "reviewer_prompt": (orchestration, "resolve_reviewer_prompt_context"),
        "initial_review_prompt": (
            initial_candidate,
            "resolve_prompt_with_output_budget_details",
        ),
        "directory": (orchestration, "audit_round_dir"),
        "close": (orchestration, "close_checkpoint"),
        "findings_close": (orchestration, "close_checkpoint"),
        "discard": (orchestration, "discard_executor_workspace_lineage"),
        "committed_discard": (orchestration, "discard_executor_workspace_lineage"),
        "log": (completion_policy, "emit_runtime_log"),
    }[boundary]
    original = getattr(module, name)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        owner_thread = get_ident()

        def blocking(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert get_ident() != owner_thread, "Storage blocked the event loop"
            assert owner_context.get() == "owner context"
            assert release.wait(timeout=2)
            result = original(*args, **kwargs)
            completed.set()
            if storage_failure:
                raise failure
            return result

        async def owner() -> None:
            owner_context.set("owner context")
            try:
                if boundary == "initial_review_prompt":
                    context.stage = context.stage.model_copy(
                        update={
                            "execution_policy": context.stage.execution_policy.model_copy(
                                update={"review_starts_with": "reviewer"}
                            )
                        }
                    )
                    context.reviewer_prompt_context = ""
                    await initial_candidate.initial_pre_review_handoff(
                        context, ReviewLoopProgress(), context.node_dir, None, 1, commit
                    )
                else:
                    await orchestration.execute_review_loop_stage(
                        context.stage,
                        context.output,
                        context.node_dir,
                        context.runtime_context,
                        context.invoker,
                        context.telemetry,
                    )
            finally:
                cleanup.append(completed.is_set())

        monkeypatch.setattr(module, name, blocking)
        task = asyncio.create_task(owner())
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel("first cancellation")
            await asyncio.sleep(0)
            task.cancel("second cancellation")
            await asyncio.sleep(0)
            if task.done():
                await task
            assert not task.done()
            assert cleanup == []
        finally:
            release.set()
            error: BaseException | None = None
            try:
                await task
            except BaseException as exc:
                error = exc
        if storage_failure:
            assert error is failure
        elif boundary in {"close", "discard", "log"}:
            assert isinstance(error, orchestration.ReviewPolicyError)
            if boundary != "log":
                assert error is policy_error
            assert events == (["close", "discard"] if boundary != "log" else ["close"])
        elif boundary == "findings_close":
            assert isinstance(error, NodeExecutionError)
            assert error.__cause__ is findings_error
            assert events == ["close"]
        else:
            assert isinstance(error, asyncio.CancelledError)
        assert ("commit" in events) is (boundary == "committed_discard")
        assert "reviewer" not in events

    asyncio.run(run())
    assert cleanup == [True]
