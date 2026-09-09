from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import pytest

from crewplane.architecture.contracts import EventType, InvocationContext
from crewplane.architecture.contracts.artifacts import build_result_filename
from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.events import ExecutionEvent
from crewplane.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    NoPresentationInvoker,
    execute_workflow,
    review_output,
)


class BarrierInvoker(NoPresentationInvoker):
    def __init__(self, node_slots: int, reviewer_slots: int) -> None:
        self.executor_barrier = asyncio.Barrier(node_slots)
        self.reviewer_barrier = asyncio.Barrier(node_slots * reviewer_slots)
        self.active: Counter[tuple[str, ProviderRole]] = Counter()
        self.max_active: Counter[tuple[str, ProviderRole]] = Counter()
        self.max_total_reviewers = 0
        self.calls: list[InvocationContext] = []

    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        del config, model, cwd, log_file
        assert invocation_context is not None
        context = invocation_context
        self.calls.append(context)
        if context.node_id == "summary":
            for node_id in ("alpha", "beta", "gamma", "delta"):
                assert f"completed {node_id}" in prompt
            output_file.write_text("all roots completed", encoding="utf-8")
            return
        key = (context.node_id, context.role)
        self.active[key] += 1
        self.max_active[key] = max(self.max_active[key], self.active[key])
        self.max_total_reviewers = max(
            self.max_total_reviewers,
            sum(
                count
                for (_, role), count in self.active.items()
                if role == ProviderRole.REVIEWER
            ),
        )
        try:
            barrier = (
                self.reviewer_barrier
                if context.role == ProviderRole.REVIEWER
                else self.executor_barrier
            )
            await barrier.wait()
            released = asyncio.Event()
            asyncio.get_running_loop().call_soon(released.set)
            await released.wait()
            content = (
                review_output(verdict="NO_FINDINGS")
                if context.role == ProviderRole.REVIEWER
                else f"completed {context.node_id}"
            )
            output_file.write_text(content, encoding="utf-8")
        finally:
            self.active[key] -= 1


@pytest.mark.parametrize(
    ("node_limit", "reviewer_limit"),
    [
        pytest.param(1, None, id="nodes-1"),
        pytest.param(2, None, id="nodes-2"),
        pytest.param(None, 1, id="reviewers-1"),
        pytest.param(None, 2, id="reviewers-2"),
        pytest.param(1, 2, id="nodes-1-reviewers-2"),
        pytest.param(2, 1, id="nodes-2-reviewers-1"),
        pytest.param(2, 2, id="nodes-2-reviewers-2"),
    ],
)
def test_workflow_respects_successful_concurrency_limits(
    tmp_path: Path,
    node_limit: int | None,
    reviewer_limit: int | None,
) -> None:
    asyncio.run(_run_limited_workflow(tmp_path, node_limit, reviewer_limit))


async def _run_limited_workflow(
    root: Path,
    node_limit: int | None,
    reviewer_limit: int | None,
) -> None:
    node_slots = node_limit or 4
    reviewer_slots = reviewer_limit or 1
    workflow = _workflow(reviewers=reviewer_limit is not None)
    config = Config(
        version=SCHEMA_VERSION,
        settings=Settings(
            max_concurrent_nodes=node_limit,
            max_parallel_invocations=reviewer_limit,
        ),
        agents={"worker": AgentConfig(cli_cmd=["mock"], default_model="test")},
    )
    invoker = BarrierInvoker(node_slots, reviewer_slots)
    output = OutputManager(workflow.name, base_dir=root)
    events: list[ExecutionEvent] = []
    async with asyncio.timeout(15):
        await execute_workflow(
            config,
            workflow,
            output,
            invoker,
            event_sink=events.append,
            suppress_progress_output=True,
        )
    assert not any(invoker.active.values())
    assert len(invoker.calls) == (21 if reviewer_limit is not None else 5)
    _assert_event_limits(events, node_slots, reviewer_limit)
    if reviewer_limit is not None:
        for node in workflow.nodes[:-1]:
            assert (
                invoker.max_active[(node.id, ProviderRole.REVIEWER)] == reviewer_limit
            )
        assert invoker.max_total_reviewers == node_slots * reviewer_limit
    for node in workflow.nodes[:-1]:
        result = output.results_dir / build_result_filename(node.id)
        assert f"completed {node.id}" in result.read_text(encoding="utf-8")
        if reviewer_limit is not None:
            assert result.read_text(encoding="utf-8").count("VERDICT: NO_FINDINGS") == 4
    summary = output.results_dir / build_result_filename("summary")
    assert "all roots completed" in summary.read_text(encoding="utf-8")
    assert events[-1].event_type == EventType.NODE_FINISHED
    assert events[-1].context.node_id == "summary"


def _assert_event_limits(
    events: list[ExecutionEvent],
    node_slots: int,
    reviewer_limit: int | None,
) -> None:
    active_nodes: set[str] = set()
    finished_nodes: set[str] = set()
    active_reviewers: dict[str, set[str]] = {}
    maximum_nodes = 0
    for event in events:
        node_id = event.context.node_id
        if event.event_type == EventType.NODE_STARTED:
            assert node_id is not None
            assert node_id not in active_nodes | finished_nodes
            if node_id == "summary":
                assert finished_nodes == {"alpha", "beta", "gamma", "delta"}
            active_nodes.add(node_id)
            maximum_nodes = max(maximum_nodes, len(active_nodes))
            assert len(active_nodes) <= node_slots
        elif event.event_type == EventType.NODE_FINISHED:
            assert node_id is not None
            active_nodes.remove(node_id)
            finished_nodes.add(node_id)
        elif event.context.role == ProviderRole.REVIEWER and event.event_type in {
            EventType.INVOCATION_STARTED,
            EventType.INVOCATION_FINISHED,
        }:
            assert node_id is not None
            assert event.context.task_id is not None
            assert reviewer_limit is not None
            active = active_reviewers.setdefault(node_id, set())
            if event.event_type == EventType.INVOCATION_STARTED:
                assert event.context.task_id not in active
                active.add(event.context.task_id)
                assert len(active) <= reviewer_limit
            else:
                active.remove(event.context.task_id)
    assert maximum_nodes == node_slots
    assert not active_nodes
    assert not any(active_reviewers.values())
    assert finished_nodes == {"alpha", "beta", "gamma", "delta", "summary"}


def _workflow(reviewers: bool) -> WorkflowPlan:
    providers = [ProviderSpec(provider="worker", role=ProviderRole.EXECUTOR)]
    if reviewers:
        providers += [
            ProviderSpec(provider="worker", role=ProviderRole.REVIEWER)
            for _ in range(4)
        ]
    nodes = [
        WorkflowNode(
            id=node_id,
            mode="sequential",
            providers=providers,
            prompt_segments=[
                PromptSegment(role=PromptSegmentRole.SHARED, content="complete work")
            ],
        )
        for node_id in ("alpha", "beta", "gamma", "delta")
    ]
    nodes.append(
        WorkflowNode(
            id="summary",
            mode="sequential",
            needs=[node.id for node in nodes],
            providers=[ProviderSpec(provider="worker", role=ProviderRole.EXECUTOR)],
            prompt_segments=[
                PromptSegment(
                    role=PromptSegmentRole.SHARED,
                    content="\n".join(f"{{{{{node.id}.output}}}}" for node in nodes),
                )
            ],
        )
    )
    return WorkflowPlan(name="success.concurrency", nodes=nodes)
