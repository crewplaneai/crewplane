import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import (
    ExecutionPolicy,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.review_loop import (
    reviewer_round as review_loop_reviewer_round,
)
from crewplane.runtime.execution.review_loop import (
    rounds as review_loop_rounds,
)
from crewplane.runtime.execution.review_loop.types import (
    ReviewerRoundRequest,
    ReviewerRoundRunResult,
)
from crewplane.runtime.execution.runtime_context import (
    agent_config_signature_from_plan,
    invoker_config_signature_from_plan,
)
from tests.helpers.artifacts import node_artifact_request
from tests.integration.runtime.execution.review_loop_rounds_support import (
    make_review_node,
    make_round_runtime_context,
    provider,
    review_output,
)


def test_parallel_reviewers_publish_only_after_every_invocation_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    fast_finished = asyncio.Event()
    release_slow = asyncio.Event()
    inspect_fast_publication: Callable[[], None] | None = None

    async def fake_guard(request):
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(review_output(), encoding="utf-8")
        if request.provider.provider == "fast":
            fast_finished.set()
            return 0
        await asyncio.wait_for(fast_finished.wait(), timeout=1.0)
        assert inspect_fast_publication is not None
        inspect_fast_publication()
        await asyncio.wait_for(release_slow.wait(), timeout=1.0)
        return 0

    monkeypatch.setattr(
        review_loop_reviewer_round, "run_provider_call_with_drift_guard", fake_guard
    )
    request = ReviewerRoundRequest(
        runtime_context=make_round_runtime_context(),
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        reviewers=(
            provider("slow", ProviderRole.REVIEWER, "slow_reviewer_0"),
            provider("fast", ProviderRole.REVIEWER, "fast_reviewer_1"),
        ),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )
    fast_output = node_dir / "fast_reviewer_1_round1.md"

    async def run_and_release() -> ReviewerRoundRunResult:
        nonlocal inspect_fast_publication

        def assert_fast_output_is_private() -> None:
            assert not fast_output.exists()

        inspect_fast_publication = assert_fast_output_is_private
        round_task = asyncio.create_task(review_loop_rounds.run_reviewer_round(request))
        await asyncio.wait_for(fast_finished.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert not fast_output.exists()
        release_slow.set()
        return await round_task

    result = asyncio.run(run_and_release())

    assert [artifact.task_id for artifact in result.outputs] == [
        "slow_reviewer_0",
        "fast_reviewer_1",
    ]
    assert fast_output.exists()


def test_peer_private_output_mutation_is_rejected_at_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node().model_copy(
        update={"execution_policy": ExecutionPolicy(continue_on_failure=True)}
    )
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    blocker_finished = asyncio.Event()
    private_outputs: dict[str, Path] = {}

    async def fake_guard(request):
        assert request.invocation_output_file is not None
        private_outputs[request.task_id] = request.invocation_output_file
        if request.task_id == "blocker_reviewer_0":
            request.invocation_output_file.write_text(
                review_output(
                    verdict="CHANGES_REQUESTED",
                    major="The candidate is unsafe.",
                ),
                encoding="utf-8",
            )
            blocker_finished.set()
            return 0
        await asyncio.wait_for(blocker_finished.wait(), timeout=1.0)
        private_outputs["blocker_reviewer_0"].write_text(
            review_output(),
            encoding="utf-8",
        )
        request.invocation_output_file.write_text(review_output(), encoding="utf-8")
        return 0

    monkeypatch.setattr(
        review_loop_reviewer_round, "run_provider_call_with_drift_guard", fake_guard
    )
    request = ReviewerRoundRequest(
        runtime_context=make_round_runtime_context(),
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        reviewers=(
            provider("blocker", ProviderRole.REVIEWER, "blocker_reviewer_0"),
            provider("peer", ProviderRole.REVIEWER, "peer_reviewer_1"),
        ),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    result = asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert result.reviewer_failure_count == 1
    assert [artifact.task_id for artifact in result.outputs] == ["peer_reviewer_1"]
    assert not (node_dir / "blocker_reviewer_0_round1.md").exists()


def test_reviewer_verdict_uses_bound_private_output_after_canonical_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    blocking_output = review_output(
        verdict="CHANGES_REQUESTED",
        major="The candidate is unsafe.",
    )

    async def fake_guard(request):
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(blocking_output, encoding="utf-8")
        return 0

    original_publish = review_loop_reviewer_round.publish_invocation_output

    def publish_then_mutate_canonical(
        invocation_output_file,
        output_file,
        publications,
        expected_signature=None,
    ):
        signature = original_publish(
            invocation_output_file,
            output_file,
            publications,
            expected_signature,
        )
        output_file.write_text(review_output(), encoding="utf-8")
        return signature

    monkeypatch.setattr(
        review_loop_reviewer_round, "run_provider_call_with_drift_guard", fake_guard
    )
    monkeypatch.setattr(
        review_loop_reviewer_round,
        "publish_invocation_output",
        publish_then_mutate_canonical,
    )
    request = ReviewerRoundRequest(
        runtime_context=make_round_runtime_context(),
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        reviewers=(provider("blocker", ProviderRole.REVIEWER, "blocker_reviewer_0"),),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    result = asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert result.outputs[0].evaluation.verdict == "CHANGES_REQUESTED"
    assert result.outputs[0].evaluation.major_issues == "The candidate is unsafe."


def test_reviewer_normalization_rejects_substituted_publication_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node().model_copy(
        update={"execution_policy": ExecutionPolicy(continue_on_failure=True)}
    )
    node_dir = output.create_node_dir(node_artifact_request(node.id))

    async def fake_guard(request):
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(
            review_output(
                verdict="CHANGES_REQUESTED",
                major="The candidate is unsafe.",
            ),
            encoding="utf-8",
        )
        return 0

    original_publish = review_loop_reviewer_round.publish_invocation_output

    def substitute_normalized_source(
        invocation_output_file,
        output_file,
        publications,
        expected_signature=None,
    ):
        invocation_output_file.write_text(review_output(), encoding="utf-8")
        return original_publish(
            invocation_output_file,
            output_file,
            publications,
            expected_signature,
        )

    monkeypatch.setattr(
        review_loop_reviewer_round, "run_provider_call_with_drift_guard", fake_guard
    )
    monkeypatch.setattr(
        review_loop_reviewer_round,
        "publish_invocation_output",
        substitute_normalized_source,
    )
    request = ReviewerRoundRequest(
        runtime_context=make_round_runtime_context(),
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        reviewers=(provider("blocker", ProviderRole.REVIEWER, "blocker_reviewer_0"),),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    result = asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert result.outputs == []
    assert result.reviewer_failure_count == 1
    assert not (node_dir / "blocker_reviewer_0_round1.md").exists()


def test_peer_canonical_precreation_is_fatal_before_round_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    runtime_context = make_round_runtime_context()
    runtime_context.plan.runtime_config_snapshot["agents"] = {
        "exec": {"cli_cmd": ["mock"], "default_model": "m1"},
        "review": {"cli_cmd": ["mock"], "default_model": "m2"},
    }
    runtime_context.plan.runtime_config_snapshot["invoker"] = {
        "capabilities": {},
        "implementation": "mock",
        "options": {},
        "option_scopes": {},
        "resolved_identity": "mock",
    }
    invoker_signature = invoker_config_signature_from_plan(runtime_context.plan)
    blocker_provider = provider(
        "exec",
        ProviderRole.REVIEWER,
        "blocker_reviewer_0",
    ).model_copy(
        update={
            "agent_config_signature": agent_config_signature_from_plan(
                runtime_context.plan,
                "exec",
                None,
            ),
            "invoker_config_signature": invoker_signature,
        }
    )
    attacker_provider = provider(
        "review",
        ProviderRole.REVIEWER,
        "attacker_reviewer_1",
    ).model_copy(
        update={
            "agent_config_signature": agent_config_signature_from_plan(
                runtime_context.plan,
                "review",
                None,
            ),
            "invoker_config_signature": invoker_signature,
        }
    )
    runtime_context.plan = runtime_context.plan.model_copy(
        update={
            "nodes": [
                runtime_context.plan.nodes[0].model_copy(
                    update={
                        "provider_records": [
                            blocker_provider,
                            attacker_provider,
                        ]
                    }
                )
            ]
        }
    )
    blocking_output = review_output(
        verdict="CHANGES_REQUESTED",
        major="The candidate is unsafe.",
    )
    blocker_output_written = asyncio.Event()
    peer_output_written = asyncio.Event()
    blocker_guard_finished = asyncio.Event()
    original_run_with_drift_guard = (
        review_loop_reviewer_round.run_provider_call_with_drift_guard
    )

    async def record_blocker_guard_finished(request):  # type: ignore[no-untyped-def]
        try:
            return await original_run_with_drift_guard(request)
        finally:
            if request.task_id == "blocker_reviewer_0":
                blocker_guard_finished.set()

    monkeypatch.setattr(
        review_loop_reviewer_round,
        "run_provider_call_with_drift_guard",
        record_blocker_guard_finished,
    )

    class PeerPrecreatingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,
        ) -> None:
            assert invocation_context is not None
            if invocation_context.task_id == "attacker_reviewer_1":
                await asyncio.wait_for(blocker_output_written.wait(), timeout=1.0)
                peer_path = node_dir / "blocker_reviewer_0_round1.md"
                peer_path.write_text(review_output(), encoding="utf-8")
                runtime_context.runtime_publications.publish(
                    peer_path,
                    file_size_and_sha256(peer_path),
                )
                output_file.write_text(review_output(), encoding="utf-8")
                peer_output_written.set()
                await asyncio.wait_for(blocker_guard_finished.wait(), timeout=1.0)
                return
            output_file.write_text(blocking_output, encoding="utf-8")
            blocker_output_written.set()
            await asyncio.wait_for(peer_output_written.wait(), timeout=1.0)

    request = ReviewerRoundRequest(
        runtime_context=runtime_context,
        node=node.model_copy(
            update={"execution_policy": ExecutionPolicy(continue_on_failure=True)}
        ),
        output=output,
        node_dir=node_dir,
        invoker=PeerPrecreatingInvoker(),
        telemetry=None,
        reviewers=(blocker_provider, attacker_provider),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    result = asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert result.reviewer_failure_count == 1
    assert [artifact.task_id for artifact in result.outputs] == ["blocker_reviewer_0"]
    canonical_blocker = node_dir / "blocker_reviewer_0_round1.md"
    assert canonical_blocker.read_text(encoding="utf-8") == blocking_output
    assert result.outputs[0].evaluation.verdict == "CHANGES_REQUESTED"
