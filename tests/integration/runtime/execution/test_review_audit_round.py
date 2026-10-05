import asyncio
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.review_checkpoint_state import CheckpointReviewerFailure
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.consensus import evaluate_review_output
from crewplane.runtime.execution.fragment_assembler import ResolvedPrompt
from crewplane.runtime.execution.review_loop import (
    audit_publication,
    audit_review_outcome,
    audit_round,
    validation,
)
from crewplane.runtime.execution.review_loop.state import render_review_inbox
from crewplane.runtime.execution.review_loop.types import (
    AuditRoundProgress,
    AuditRoundRequest,
    ExecutorRoundArtifact,
    ExecutorRoundRequest,
    ExecutorRoundRunResult,
    ReviewerRoundArtifact,
    ReviewerRoundRequest,
    ReviewerRoundRunResult,
)
from tests.helpers.artifacts import node_artifact_request
from tests.integration.runtime.execution.review_loop_rounds_support import (
    make_review_node,
    make_round_runtime_context,
    provider_failure,
    review_output,
)


@pytest.fixture
def audit_request(tmp_path: Path) -> AuditRoundRequest:
    node = make_review_node()
    output = OutputManager("workflow", base_dir=tmp_path)
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    executor, reviewer = node.provider_records
    candidate = ExecutorRoundArtifact(
        executor, executor.task_id, "Candidate", node_dir / "candidate.md", None, 1
    )
    return AuditRoundRequest(
        runtime_context=make_round_runtime_context(),
        stage=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        executors=(executor,),
        reviewers=(reviewer,),
        executor_prompt="Execute task.",
        reviewer_prompt_context="Review task.",
        audit_dir=node_dir,
        remediation_depth=1,
        initial_executor_outputs=[candidate],
        audit_round_num=None,
    )


def reviewer_run(
    request: AuditRoundRequest, round_num: int, approved: bool = False
) -> ReviewerRoundRunResult:
    reviewer = request.reviewers[0]
    evaluation = evaluate_review_output(
        review_output()
        if approved
        else review_output("CHANGES_REQUESTED", "- Fix the missing branch.")
    )
    return ReviewerRoundRunResult(
        [
            ReviewerRoundArtifact(
                reviewer,
                reviewer.task_id,
                evaluation,
                request.audit_dir / f"review_round{round_num}.md",
                None,
                round_num,
            )
        ],
        drift_warning_count=2,
    )


def test_audit_orders_publication_transitions_and_progress(
    audit_request: AuditRoundRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = audit_request
    progress = AuditRoundProgress(request.initial_executor_outputs)
    request.progress = progress
    events: list[tuple[str, int]] = []

    def publish() -> None:
        events.append(("status", progress.last_round_num))

    async def transition(phase: str, round_num: int) -> None:
        events.append((phase, round_num))
        if phase == "executors":
            assert (
                progress.previous_executor_outputs is request.initial_executor_outputs
            )
            assert "Fix the missing branch" in progress.previous_review_packet

    async def execute(round_request: ExecutorRoundRequest) -> ExecutorRoundRunResult:
        events.append(("execute", round_request.round_num))
        assert (
            round_request.previous_executor_outputs is request.initial_executor_outputs
        )
        assert round_request.previous_review_packet == progress.previous_review_packet
        return ExecutorRoundRunResult(
            [
                replace(
                    request.initial_executor_outputs[0],
                    content="Revised candidate",
                    round_num=2,
                )
            ],
            drift_warning_count=3,
        )

    async def review(round_request: ReviewerRoundRequest) -> ReviewerRoundRunResult:
        events.append(("review", round_request.round_num))
        return reviewer_run(
            request, round_request.round_num, round_request.round_num == 2
        )

    persist = audit_round.persist_round_review_inbox

    def inbox(request, progress, outputs, round_num) -> None:
        assert progress.latest_reviewer_outputs is outputs
        assert progress.selected_round_num == round_num
        persist(request, progress, outputs, round_num)
        events.append(("inbox", round_num))

    request.publish_status = publish
    request.commit_transition = transition
    monkeypatch.setattr(audit_round, "run_executor_round", execute)
    monkeypatch.setattr(audit_round, "run_reviewer_round", review)
    monkeypatch.setattr(audit_round, "persist_round_review_inbox", inbox)

    result = asyncio.run(audit_round.execute_single_audit_round(request))

    assert events == [
        ("status", 1),
        ("reviewers", 1),
        ("review", 1),
        ("inbox", 1),
        ("executors", 2),
        ("status", 2),
        ("execute", 2),
        ("reviewers", 2),
        ("review", 2),
        ("inbox", 2),
    ]
    assert result.consensus_reached
    assert not result.clean_fresh_approval
    assert result.latest_executor_outputs is progress.executor_outputs
    assert result.latest_reviewer_outputs is progress.latest_reviewer_outputs
    assert result.last_round_num == result.selected_round_num == 2
    assert result.artifact_drift_warning_count == 7
    assert progress.previous_executor_outputs is request.initial_executor_outputs
    inbox_path = request.audit_dir / "review-state" / "review-inbox-round-1.md"
    expected = render_review_inbox(
        request.stage.id,
        None,
        1,
        request.initial_executor_outputs,
        None,
        reviewer_run(request, 1).outputs,
    )
    assert inbox_path.read_text() == expected + "\n"
    assert not (inbox_path.parent / "review-inbox-round-2.md").exists()


def test_resumed_review_skips_executor_validation_and_transition(
    audit_request: AuditRoundRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = audit_request
    request.start_phase = "reviewers"
    request.start_round = 2
    candidate = [replace(request.initial_executor_outputs[0], content="", round_num=2)]
    progress = AuditRoundProgress(candidate)
    request.progress = progress

    async def unexpected(*args) -> None:
        pytest.fail(f"Unexpected executor or checkpoint call: {args}")

    async def review(round_request: ReviewerRoundRequest) -> ReviewerRoundRunResult:
        assert round_request.round_num == 2
        return reviewer_run(request, 2, approved=True)

    request.commit_transition = unexpected
    monkeypatch.setattr(audit_round, "run_executor_round", unexpected)
    monkeypatch.setattr(audit_round, "run_reviewer_round", review)

    result = asyncio.run(audit_round.execute_single_audit_round(request))

    assert result.consensus_reached
    assert not result.clean_fresh_approval
    assert result.latest_executor_outputs is candidate
    assert result.invalid_candidate_round_count == 0


@pytest.mark.parametrize(
    "failure", [OSError("storage failed"), asyncio.CancelledError()]
)
def test_inbox_failure_keeps_completed_review_and_publishes_final_status(
    audit_request: AuditRoundRequest,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    request = audit_request
    progress = AuditRoundProgress(request.initial_executor_outputs)
    request.progress = progress
    run = reviewer_run(request, 1)
    publications: list[tuple[int, int]] = []

    def publish() -> None:
        publications.append((progress.last_round_num, progress.selected_round_num))

    async def review(round_request: ReviewerRoundRequest) -> ReviewerRoundRunResult:
        assert round_request.round_num == 1
        return run

    def fail(*args) -> None:
        assert args[1] is progress
        raise failure

    request.publish_status = publish
    monkeypatch.setattr(audit_round, "run_reviewer_round", review)
    monkeypatch.setattr(audit_round, "persist_round_review_inbox", fail)

    with pytest.raises(type(failure)) as caught:
        asyncio.run(audit_round.execute_single_audit_round(request))

    assert caught.value is failure
    assert publications == [(1, 0), (1, 1)]
    assert progress.latest_valid_executor_outputs is request.initial_executor_outputs
    assert progress.latest_reviewer_outputs is run.outputs
    assert progress.artifact_drift_warning_count == 2
    assert progress.previous_review_packet is None


@pytest.mark.parametrize("recoverable", [False, True])
def test_remediation_failure_preserves_exception_or_recovers_latest_candidate(
    audit_request: AuditRoundRequest, monkeypatch: pytest.MonkeyPatch, recoverable: bool
) -> None:
    request = audit_request
    request.start_round = 2
    latest = request.initial_executor_outputs
    progress = AuditRoundProgress(latest, latest_valid_executor_outputs=latest)
    request.progress = progress
    failure = provider_failure(
        "provider_session_context_exhausted" if recoverable else "quota_or_rate_limit",
        "provider_session" if recoverable else "provider_transport",
    )
    transitions: list[tuple[str, int]] = []

    async def transition(phase: str, round_num: int) -> None:
        transitions.append((phase, round_num))

    async def execute(round_request: ExecutorRoundRequest) -> ExecutorRoundRunResult:
        assert round_request.round_num == 2
        raise failure

    request.commit_transition = transition
    monkeypatch.setattr(audit_round, "run_executor_round", execute)

    if recoverable:
        result = asyncio.run(audit_round.execute_single_audit_round(request))
        assert not result.consensus_reached
        assert result.latest_executor_outputs is latest
        assert request.rejected_invocations == [
            (None, 2, {request.executors[0].task_id}, "remediation_context_exhausted")
        ]
    else:
        with pytest.raises(type(failure)) as caught:
            asyncio.run(audit_round.execute_single_audit_round(request))
        assert caught.value is failure
        assert request.rejected_invocations == []
    assert transitions == []


@pytest.mark.parametrize("boundary", ["inbox", "status"])
@pytest.mark.parametrize("storage_failure", [False, True])
def test_publication_yields_and_finishes_before_repeated_cancellation(
    audit_request: AuditRoundRequest,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    storage_failure: bool,
) -> None:
    request = audit_request
    progress = AuditRoundProgress(request.initial_executor_outputs)
    request.progress = progress
    release = Event()
    completed = Event()
    failure = OSError("publication failed")
    statuses: list[tuple[int, int]] = []
    worker_threads: list[int] = []
    persist = audit_round.persist_round_review_inbox

    async def run() -> None:
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        owner_thread = get_ident()

        def block() -> None:
            worker_threads.append(get_ident())
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=2), "Publication blocked the event loop"
            completed.set()
            if storage_failure:
                raise failure

        def publish() -> None:
            if boundary == "status" and not statuses:
                statuses.append((progress.last_round_num, progress.selected_round_num))
                block()
                return
            if boundary == "inbox" and progress.selected_round_num:
                assert completed.is_set()
            statuses.append((progress.last_round_num, progress.selected_round_num))

        def inbox(request, progress, outputs, round_num) -> None:
            block()
            persist(request, progress, outputs, round_num)

        async def review(round_request: ReviewerRoundRequest) -> ReviewerRoundRunResult:
            assert round_request.round_num == 1
            return reviewer_run(request, 1)

        request.publish_status = publish
        monkeypatch.setattr(audit_round, "run_reviewer_round", review)
        if boundary == "inbox":
            monkeypatch.setattr(audit_round, "persist_round_review_inbox", inbox)
        task = asyncio.create_task(audit_round.execute_single_audit_round(request))
        expected_error = OSError if storage_failure else asyncio.CancelledError
        error: BaseException | None = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert len(worker_threads) == 1
            assert worker_threads[0] != owner_thread
            task.cancel("first cancellation")
            await asyncio.sleep(0)
            task.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            assert statuses == [(1, 0)]
            assert progress.previous_review_packet is None
        finally:
            release.set()
            try:
                await task
            except (OSError, asyncio.CancelledError) as exc:
                error = exc
        assert isinstance(error, expected_error)
        if storage_failure:
            assert error is failure
        else:
            assert str(error) == "second cancellation"
        assert completed.is_set()
        assert statuses == [(1, 0), (1, 1 if boundary == "inbox" else 0)]
        inbox_path = request.audit_dir / "review-state" / "review-inbox-round-1.md"
        assert inbox_path.exists() == (boundary == "inbox" and not storage_failure)
        assert list(request.audit_dir.rglob("*.tmp")) == []
        assert progress.latest_valid_executor_outputs is (
            request.initial_executor_outputs if boundary == "inbox" else None
        )

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["warning", "console"])
@pytest.mark.parametrize("io_failure", [False, True])
def test_review_outcome_io_order_and_progress(
    audit_request: AuditRoundRequest,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    io_failure: bool,
) -> None:
    request = audit_request
    request.start_phase = "reviewers"
    request.remediation_depth = 0
    run = reviewer_run(request, 1, approved=boundary == "console")
    progress = AuditRoundProgress(
        request.initial_executor_outputs,
        previous_review_packet="Previous feedback",
        previous_executor_fingerprint="previous candidate",
        previous_unresolved_fingerprints=(
            run.outputs[0].evaluation.unresolved_fingerprints
        ),
    )
    request.progress = progress
    events: list[str] = []
    failure = OSError("review outcome I/O failed")
    persist = audit_round.persist_round_review_inbox

    def report() -> None:
        assert progress.latest_reviewer_outputs is run.outputs
        assert progress.selected_round_num == 1
        assert progress.previous_review_packet == "Previous feedback"
        assert progress.stall.candidate_fingerprint is None
        events.append(boundary)
        if io_failure:
            raise failure

    def log(telemetry, **kwargs) -> None:
        assert telemetry is request.telemetry
        assert kwargs["operation"] == "review_stall_detection"
        assert kwargs["attributes"] == {
            "executor_output_changed": True,
            "repeated_fingerprint_count": 1,
            "current_unresolved_issue_count": 1,
        }
        assert kwargs["context"].round_num == 1
        report()

    class ApprovalConsole:
        def print(self, message: str) -> None:
            assert message == "[green bold]Consensus reached in round 1![/]"
            report()

    def console(telemetry) -> ApprovalConsole:
        assert telemetry is request.telemetry
        return ApprovalConsole()

    def publish() -> None:
        events.append("status")

    def inbox(request, progress, outputs, round_num) -> None:
        persist(request, progress, outputs, round_num)
        events.append("inbox")

    async def review(round_request: ReviewerRoundRequest) -> ReviewerRoundRunResult:
        assert round_request.round_num == 1
        events.append("review")
        return run

    request.publish_status = publish
    monkeypatch.setattr(audit_round, "run_reviewer_round", review)
    monkeypatch.setattr(audit_round, "persist_round_review_inbox", inbox)
    monkeypatch.setattr(validation, "emit_runtime_log", log)
    monkeypatch.setattr(audit_review_outcome, "execution_console", console)
    if io_failure:
        with pytest.raises(OSError) as caught:
            asyncio.run(audit_round.execute_single_audit_round(request))
        assert caught.value is failure
    else:
        result = asyncio.run(audit_round.execute_single_audit_round(request))
        assert result.consensus_reached == (boundary == "console")
        assert result.clean_fresh_approval == (boundary == "console")
        assert result.latest_reviewer_outputs is run.outputs
        assert result.latest_executor_outputs is request.initial_executor_outputs
    assert events == ["status", "review", "inbox", boundary] + (
        ["status"] if io_failure else []
    )
    assert progress.previous_review_packet == (
        "Previous feedback"
        if io_failure or boundary == "console"
        else audit_round.render_unresolved_review_packet(run.outputs)
    )
    assert progress.stall.candidate_fingerprint == (
        None
        if io_failure
        else audit_round.build_executor_output_fingerprint(
            request.initial_executor_outputs
        )
    )


@pytest.mark.parametrize("boundary", ["warning", "console"])
@pytest.mark.parametrize("io_failure", [False, True])
def test_review_outcome_io_yields_and_drains_before_cancellation(
    audit_request: AuditRoundRequest,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    io_failure: bool,
) -> None:
    request = audit_request
    request.start_phase = "reviewers"
    request.remediation_depth = 0
    run = reviewer_run(request, 1, approved=boundary == "console")
    progress = AuditRoundProgress(
        request.initial_executor_outputs,
        previous_review_packet="Previous feedback",
        previous_executor_fingerprint="previous candidate",
        previous_unresolved_fingerprints=(
            run.outputs[0].evaluation.unresolved_fingerprints
        ),
    )
    request.progress = progress
    release = Event()
    completed = Event()
    failure = OSError("review outcome I/O failed")
    statuses: list[tuple[bool, int]] = []
    context = ContextVar("review-outcome-context", default="missing")

    async def exercise() -> None:
        context.set("owner context")
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        owner_thread = get_ident()

        def block() -> None:
            assert get_ident() != owner_thread, (
                "Review outcome I/O blocked the event loop"
            )
            assert context.get() == "owner context"
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=2)
            completed.set()
            if io_failure:
                raise failure

        def log(telemetry, **kwargs) -> None:
            assert telemetry is request.telemetry
            assert kwargs["operation"] == "review_stall_detection"
            block()

        class ApprovalConsole:
            def print(self, message: str) -> None:
                assert message == "[green bold]Consensus reached in round 1![/]"
                block()

        def console(telemetry) -> ApprovalConsole:
            assert telemetry is request.telemetry
            return ApprovalConsole()

        def publish() -> None:
            statuses.append((completed.is_set(), progress.selected_round_num))

        async def review(round_request: ReviewerRoundRequest) -> ReviewerRoundRunResult:
            assert round_request.round_num == 1
            return run

        request.publish_status = publish
        monkeypatch.setattr(audit_round, "run_reviewer_round", review)
        monkeypatch.setattr(validation, "emit_runtime_log", log)
        monkeypatch.setattr(audit_review_outcome, "execution_console", console)
        task = asyncio.create_task(audit_round.execute_single_audit_round(request))
        error: BaseException | None = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel("first cancellation")
            await asyncio.sleep(0)
            task.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            assert statuses == [(False, 0)]
            assert progress.previous_review_packet == "Previous feedback"
            assert progress.stall.candidate_fingerprint is None
        finally:
            release.set()
            try:
                await task
            except (OSError, asyncio.CancelledError) as exc:
                error = exc
        assert completed.is_set()
        assert statuses == [(False, 0), (True, 1)]
        assert (
            progress.latest_valid_executor_outputs is request.initial_executor_outputs
        )
        assert progress.latest_reviewer_outputs is run.outputs
        assert progress.artifact_drift_warning_count == 2
        assert progress.previous_review_packet == "Previous feedback"
        assert progress.stall.candidate_fingerprint is None
        inbox = request.audit_dir / "review-state" / "review-inbox-round-1.md"
        assert inbox.exists() == (boundary == "warning")
        assert list(request.audit_dir.rglob("*.tmp")) == []
        if io_failure:
            assert error is failure
        else:
            assert isinstance(error, asyncio.CancelledError)
            assert str(error) == "second cancellation"

    asyncio.run(exercise())


@pytest.mark.parametrize("failure_phase", ["prompt", "review", "returned", None])
def test_dynamic_prompt_and_reviewer_failure_progress(
    audit_request: AuditRoundRequest,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str | None,
) -> None:
    request = audit_request
    request.reviewer_prompt_context = ""
    progress = AuditRoundProgress(
        request.initial_executor_outputs, previous_review_packet="Earlier feedback"
    )
    original = replace(progress)
    failure = OSError("resolution or invocation failed")
    run = reviewer_run(request, 2, approved=True)
    if failure_phase == "returned":
        run.reviewer_failure_count = 1
        run.failures = [
            CheckpointReviewerFailure(
                task_id=request.reviewers[0].task_id,
                role=ProviderRole.REVIEWER,
                audit=1,
                local_round=2,
                failure_kind="provider_transport",
                warning="Reviewer failed",
            )
        ]
    events: list[str] = []

    def resolve(runtime_context, stage, output, **kwargs) -> ResolvedPrompt:
        assert runtime_context is request.runtime_context
        assert stage is request.stage and output is request.output
        assert kwargs["role"] is ProviderRole.REVIEWER
        assert kwargs["telemetry"] is request.telemetry
        context = kwargs["workspace_candidate_context"]
        assert context.role_label is ProviderRole.REVIEWER
        assert context.round_num == 2 and context.audit_round_num is None
        events.append("prompt")
        if failure_phase == "prompt":
            raise failure
        return ResolvedPrompt("Dynamic prompt", request.reviewer_prompt_workspace_files)

    async def review(round_request: ReviewerRoundRequest) -> ReviewerRoundRunResult:
        assert round_request.round_num == 2
        assert round_request.reviewer_prompt_context == "Dynamic prompt"
        assert (
            round_request.reviewer_prompt_workspace_files
            is request.reviewer_prompt_workspace_files
        )
        assert "Candidate" in round_request.review_context
        assert round_request.previous_review_packet == "Earlier feedback"
        events.append("review")
        if failure_phase == "review":
            raise failure
        return run

    monkeypatch.setattr(
        audit_round, "resolve_prompt_with_output_budget_details", resolve
    )
    monkeypatch.setattr(audit_round, "run_reviewer_round", review)
    if failure_phase in {"prompt", "review"}:
        with pytest.raises(OSError) as caught:
            asyncio.run(
                audit_round.run_review_phase(request, progress, "fingerprint", 2)
            )
        assert caught.value is failure
        assert progress == original
        assert events == (
            ["prompt"] if failure_phase == "prompt" else ["prompt", "review"]
        )
        return
    state = asyncio.run(
        audit_round.run_review_phase(request, progress, "fingerprint", 2)
    )
    assert events == ["prompt", "review"]
    assert state.reviewer_outputs is progress.latest_reviewer_outputs is run.outputs
    assert state.current_executor_fingerprint == "fingerprint"
    assert progress.latest_valid_executor_outputs is request.initial_executor_outputs
    assert progress.selected_round_num == 2
    assert progress.reviewer_failures is run.failures
    assert progress.artifact_drift_warning_count == 2
    assert progress.previous_review_packet == "Earlier feedback"
    assert audit_round.review_phase_reached_consensus(request, state, 2) == (
        failure_phase is None
    )


@pytest.mark.parametrize("kind", ["invalid", "unchanged", "context"])
@pytest.mark.parametrize("storage_failure", [False, True])
def test_lineage_rejection_precedes_fallback_and_warning(
    audit_request: AuditRoundRequest,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    storage_failure: bool,
) -> None:
    request = audit_request
    request.start_round = 2
    latest = request.initial_executor_outputs
    candidate = [replace(latest[0], content="" if kind == "invalid" else "Candidate")]
    progress = AuditRoundProgress(
        candidate,
        latest_valid_executor_outputs=latest,
        previous_review_packet="Fix the branch",
        previous_executor_fingerprint=audit_round.build_executor_output_fingerprint(
            latest
        ),
    )
    progress.stall.consecutive_round_count = 1
    request.progress = progress
    failure = OSError("lineage storage failed")
    events: list[str] = []

    async def execute(round_request: ExecutorRoundRequest) -> ExecutorRoundRunResult:
        assert round_request.recovery_attempt
        if kind == "context":
            raise provider_failure(
                "provider_session_context_exhausted", "provider_session"
            )
        return ExecutorRoundRunResult(candidate, drift_warning_count=3)

    def discard(output, stage, task_ids, audit, round_num, reason) -> None:
        assert output is request.output and stage is request.stage
        assert task_ids == {request.executors[0].task_id}
        assert audit is None and round_num == 2
        assert (
            reason
            == {
                "invalid": "invalid_candidate.empty",
                "unchanged": "no_progress_candidate",
                "context": "remediation_context_exhausted",
            }[kind]
        )
        assert progress.executor_outputs is candidate
        assert progress.invalid_candidate_round_count == (kind == "invalid")
        assert progress.no_progress_round_count == (kind == "unchanged")
        assert progress.stall.consecutive_round_count == (0 if kind == "invalid" else 1)
        events.append("discard")
        if storage_failure:
            raise failure

    def warn(*args, **kwargs) -> None:
        del args, kwargs
        events.append("warning")

    monkeypatch.setattr(audit_round, "run_executor_round", execute)
    monkeypatch.setattr(audit_round, "discard_executor_workspace_lineage", discard)
    monkeypatch.setattr(audit_round, "emit_invalid_candidate_warning", warn)
    monkeypatch.setattr(audit_round, "emit_no_progress_warning", warn)
    monkeypatch.setattr(
        audit_round, "emit_remediation_context_exhaustion_warning", warn
    )
    if storage_failure:
        with pytest.raises(OSError) as caught:
            asyncio.run(audit_round.execute_single_audit_round(request))
        assert caught.value is failure
        assert events == ["discard"]
        assert progress.executor_outputs is candidate
    else:
        result = asyncio.run(audit_round.execute_single_audit_round(request))
        assert not result.consensus_reached
        assert result.latest_executor_outputs is latest
        assert progress.executor_outputs is latest
        assert events == ["discard", "warning"]
    assert request.rejected_invocations == []


@pytest.mark.parametrize("storage_failure", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_runner_shutdown_drains_publication_before_owner_cleanup(
    storage_failure: bool,
    interrupted: bool,
) -> None:
    release = Event()
    completed = Event()
    failure = OSError("publication failed during shutdown")
    cleanup: list[bool] = []
    errors: list[BaseException] = []
    tasks: list[asyncio.Task[None]] = []

    async def main() -> None:
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def publish() -> None:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=2)
            completed.set()
            if storage_failure:
                raise failure

        async def owner() -> None:
            try:
                await audit_publication.complete_audit_publication(publish)
            except (OSError, asyncio.CancelledError) as exc:
                errors.append(exc)
            finally:
                cleanup.append(completed.is_set())

        async def release_on_shutdown() -> None:
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                release.set()

        tasks.extend(
            [asyncio.create_task(owner()), asyncio.create_task(release_on_shutdown())]
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        if interrupted:
            raise KeyboardInterrupt("runner interrupted")

    try:
        if interrupted:
            with pytest.raises(KeyboardInterrupt, match="runner interrupted"):
                asyncio.run(main())
        else:
            asyncio.run(main())
    finally:
        release.set()
    assert cleanup == [True]
    assert len(errors) == 1
    if storage_failure:
        assert errors[0] is failure
    else:
        assert isinstance(errors[0], asyncio.CancelledError)


@pytest.mark.parametrize("boundary", ["prompt", "invalid", "unchanged", "context"])
@pytest.mark.parametrize("storage_failure", [False, True])
def test_audit_io_yields_and_retains_ownership_until_cancellation(
    audit_request: AuditRoundRequest,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    storage_failure: bool,
) -> None:
    request = audit_request
    request.start_round = 1 if boundary == "prompt" else 2
    request.reviewer_prompt_context = "" if boundary == "prompt" else "Review"
    latest = request.initial_executor_outputs
    candidate = [
        replace(latest[0], content="" if boundary == "invalid" else "Candidate")
    ]
    progress = AuditRoundProgress(
        candidate,
        latest_valid_executor_outputs=latest,
        previous_review_packet="Previous feedback",
        previous_executor_fingerprint=audit_round.build_executor_output_fingerprint(
            latest
        ),
    )
    request.progress = progress
    release = Event()
    completed = Event()
    failure = OSError("audit I/O failed")
    statuses: list[bool] = []
    context = ContextVar("audit-test-context", default="missing")

    async def run() -> None:
        context.set("owner context")
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        owner_thread = get_ident()

        def block(*args, **kwargs) -> ResolvedPrompt:
            del args, kwargs
            assert get_ident() != owner_thread
            assert context.get() == "owner context"
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=2), "Audit I/O blocked the event loop"
            completed.set()
            if storage_failure:
                raise failure
            return ResolvedPrompt("Resolved prompt")

        def publish() -> None:
            statuses.append(completed.is_set())

        async def execute(
            round_request: ExecutorRoundRequest,
        ) -> ExecutorRoundRunResult:
            assert round_request.round_num == request.start_round
            if boundary == "context":
                raise provider_failure(
                    "provider_session_context_exhausted", "provider_session"
                )
            return ExecutorRoundRunResult(candidate, drift_warning_count=0)

        async def review(
            round_request: ReviewerRoundRequest,
        ) -> ReviewerRoundRunResult:
            pytest.fail(
                f"Review round {round_request.round_num} started after cancelled audit I/O"
            )

        request.publish_status = publish
        monkeypatch.setattr(audit_round, "run_executor_round", execute)
        monkeypatch.setattr(audit_round, "run_reviewer_round", review)
        monkeypatch.setattr(
            audit_round,
            "resolve_prompt_with_output_budget_details"
            if boundary == "prompt"
            else "discard_executor_workspace_lineage",
            block,
        )
        task = asyncio.create_task(audit_round.execute_single_audit_round(request))
        error: BaseException | None = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel("first cancellation")
            await asyncio.sleep(0)
            task.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not task.done()
            assert statuses == [False]
            assert progress.executor_outputs is candidate
            assert progress.previous_review_packet == "Previous feedback"
            assert progress.selected_round_num == 0
        finally:
            release.set()
            try:
                await task
            except (OSError, asyncio.CancelledError) as exc:
                error = exc
        assert completed.is_set()
        assert statuses == [False, True]
        assert progress.executor_outputs is (
            latest if boundary != "prompt" and not storage_failure else candidate
        )
        if storage_failure:
            assert error is failure
        else:
            assert isinstance(error, asyncio.CancelledError)
            assert str(error) == "second cancellation"

    asyncio.run(run())
