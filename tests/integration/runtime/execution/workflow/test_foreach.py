import asyncio
import json
from pathlib import Path

import pytest

from crewplane.architecture.contracts import EventType
from crewplane.artifacts import OutputManager
from crewplane.artifacts.expansions import reconstruct_expansions
from crewplane.runtime.execution import execute_workflow
from tests.helpers.foreach import (
    compile_foreach_file,
    compile_literal_foreach,
    copy_foreach_fixture,
    freeze_foreach,
    workflow_document,
    write_workflow_document,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
)


def test_dynamic_foreach_commits_before_children_and_joins_in_item_order(
    tmp_path: Path,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    preview = compile_foreach_file(path)
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)
    base_bytes = (output.stages_dir / "preflight/execution-plan.json").read_bytes()
    events = []
    invoker = MockAgentInvoker(
        outputs=[
            '<!-- findings -->\n[{"chunk":2},{"chunk":1}]\n<!-- /findings -->',
            "<!-- findings -->second<!-- /findings -->",
            "<!-- findings -->first<!-- /findings -->",
            "summary",
        ]
    )
    effective = asyncio.run(
        execute_workflow(
            plan, output, invoker, preview.secret_context, event_sink=events.append
        )
    )
    assert [call["node_id"] for call in invoker.calls] == [
        "planner",
        "chunks.i0.part.analyze",
        "chunks.i1.part.analyze",
        "summarize",
    ]
    assert "concise" in invoker.calls[1]["prompt"]
    assert "Review both chunks." in invoker.calls[1]["prompt"]
    assert (
        output.stages_dir / "preflight/execution-plan.json"
    ).read_bytes() == base_bytes
    expansion = output.stages_dir / "preflight/expansions/chunks"
    assert (expansion / "items/0.json").read_bytes() == b'{"chunk":2}'
    assert (expansion / "items/1.json").read_bytes() == b'{"chunk":1}'
    index = json.loads((output.results_dir / "chunks-result.md").read_bytes())
    assert [entry["item"] for entry in index] == [{"chunk": 2}, {"chunk": 1}]
    assert [entry["results"][0]["node_id"] for entry in index] == [
        "chunks.i0.part.analyze",
        "chunks.i1.part.analyze",
    ]
    for entry in index:
        for kind in ("output", "findings"):
            descriptor = entry["results"][0][kind]
            assert (
                output.results_dir / descriptor["path"]
            ).stat().st_size == descriptor["size_bytes"]
    topology_index = next(
        i
        for i, event in enumerate(events)
        if event.event_type == EventType.TOPOLOGY_EXPANDED
    )
    first_child = next(
        i
        for i, event in enumerate(events)
        if (event.context.node_id or "").startswith("chunks.i")
    )
    assert topology_index < first_child
    reconstructed = reconstruct_expansions(plan, output.stages_dir)
    assert not reconstructed.invalid_groups
    assert reconstructed.plan == effective


@pytest.mark.parametrize("items,count", [("[]", 0), ("[{}]", 1), ("[{}, {}, {}]", 3)])
def test_literal_instances_and_empty_join(
    tmp_path: Path, items: str, count: int
) -> None:
    preview = compile_literal_foreach(tmp_path, items)
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)
    invoker = MockAgentInvoker()
    effective = asyncio.run(
        execute_workflow(plan, output, invoker, preview.secret_context)
    )
    assert len(invoker.calls) == count * 2
    assert len(effective.nodes) == 1 + count * 3
    assert (
        len(json.loads((output.results_dir / "chunks-result.md").read_bytes())) == count
    )


def test_two_ready_groups_use_one_scheduler_with_concurrency_one(
    tmp_path: Path,
) -> None:
    compile_literal_foreach(tmp_path, "[{}]")
    parent = tmp_path / "parent.md"
    payload, body = workflow_document(parent)
    payload["nodes"].append({**payload["nodes"][0], "id": "other"})
    write_workflow_document(parent, payload, body + "\n## other\n")
    preview = compile_foreach_file(parent)
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)
    invoker = MockAgentInvoker()
    effective = asyncio.run(
        execute_workflow(plan, output, invoker, preview.secret_context)
    )
    assert set(effective.expansions) == {"chunks", "other"}
    assert len(invoker.calls) == 4


def test_chained_group_consumes_raw_ordered_index(tmp_path: Path) -> None:
    compile_literal_foreach(tmp_path, "[{}]")
    parent = tmp_path / "parent.md"
    payload, body = workflow_document(parent)
    payload["nodes"].append(
        {
            **payload["nodes"][0],
            "id": "next",
            "needs": ["chunks"],
            "items": "{{chunks.output}}",
            "max_items": 1,
        }
    )
    write_workflow_document(parent, payload, body + "\n## next\n")
    preview = compile_foreach_file(parent)
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)
    effective = asyncio.run(
        execute_workflow(plan, output, MockAgentInvoker(), preview.secret_context)
    )
    assert len(effective.expansions) == 2
    assert len(effective.nodes) == 8
    result = json.loads((output.results_dir / "next-result.md").read_bytes())
    assert result[0]["item"]["index"] == 0
    assert result[0]["item"]["results"][0]["node_id"].startswith("chunks.i0.")


def test_unused_item_slot_gates_roots_without_becoming_a_terminal_result(
    tmp_path: Path,
) -> None:
    compile_literal_foreach(tmp_path, "[{}]")
    child = tmp_path / "child.md"
    payload, body = workflow_document(child)
    payload["nodes"] = [payload["nodes"][0], payload["nodes"][2]]
    write_workflow_document(child, payload, "## independent\nNo item injection.")
    preview = compile_foreach_file(tmp_path / "parent.md")
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)
    effective = asyncio.run(
        execute_workflow(plan, output, MockAgentInvoker(), preview.secret_context)
    )
    group = next(node for node in effective.nodes if node.id == "chunks")
    assert group.dependencies == ["chunks.i0.independent"]
    result = json.loads((output.results_dir / "chunks-result.md").read_bytes())
    assert [node["local_node_id"] for node in result[0]["results"]] == ["independent"]


@pytest.mark.parametrize("change", ["collection", "shared"])
def test_runtime_data_changes_expansion_identity_without_changing_root_signature(
    tmp_path: Path, change: str
) -> None:
    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    payload["nodes"][2]["inputs"]["request"] = "planner"
    write_workflow_document(path, payload, body)
    preview = compile_foreach_file(path)
    digests = []
    for ordinal in range(2):
        output = OutputManager("foreach", base_dir=tmp_path)
        plan = freeze_foreach(preview, output)
        items = [{"chunk": ordinal if change == "collection" else 0}]
        shared = (
            f"Shared revision {ordinal}" if change == "shared" else "Shared request"
        )
        invoker = MockAgentInvoker(
            outputs=[
                shared
                + "\n<!-- findings -->"
                + json.dumps(items)
                + "<!-- /findings -->",
                "<!-- findings -->done<!-- /findings -->",
                "summary",
            ]
        )
        effective = asyncio.run(
            execute_workflow(plan, output, invoker, preview.secret_context)
        )
        assert effective.workflow_signature == preview.workflow_signature
        assert shared in invoker.calls[1]["prompt"]
        digests.append(effective.expansions["chunks"].digest)
    assert digests[0] != digests[1]


@pytest.mark.parametrize(
    "collection", ['[{"x":1,"x":2}]', "```json\n[]\n```", "[1]", '[{"x":NaN}]']
)
def test_invalid_runtime_collection_blocks_children_but_keeps_independent_work(
    tmp_path: Path, collection: str
) -> None:
    from crewplane.runtime.execution.errors import WorkflowExecutionError

    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    payload["nodes"].append(
        {"id": "independent", "mode": "parallel", "providers": ["alpha"]}
    )
    write_workflow_document(path, payload, body + "\n## independent\nIndependent work.")
    preview = compile_foreach_file(path)
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)
    invoker = MockAgentInvoker(
        outputs=[
            "<!-- findings -->" + collection + "<!-- /findings -->",
            "Independent complete",
        ]
    )
    with pytest.raises(WorkflowExecutionError, match="chunks"):
        asyncio.run(execute_workflow(plan, output, invoker, preview.secret_context))
    assert {call["node_id"] for call in invoker.calls} == {"planner", "independent"}
    assert not (
        output.stages_dir / "preflight/expansions/chunks/expansion.json"
    ).exists()


def test_uncommitted_expansion_never_invokes_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crewplane.runtime.execution.errors import WorkflowExecutionError

    preview = compile_literal_foreach(tmp_path, "[{}]")
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)
    write = output.write_preflight_json

    def fail_commit(path: str, payload: object) -> Path:
        if path.endswith("/expansion.json"):
            raise OSError("commit unavailable")
        return write(path, payload)

    monkeypatch.setattr(output, "write_preflight_json", fail_commit)
    invoker = MockAgentInvoker()
    with pytest.raises(WorkflowExecutionError, match="commit unavailable"):
        asyncio.run(execute_workflow(plan, output, invoker, preview.secret_context))
    assert not invoker.calls
    reconstructed = reconstruct_expansions(plan, output.stages_dir)
    assert reconstructed.plan == plan
    assert reconstructed.invalid_groups == {"chunks"}


@pytest.mark.race
def test_children_finish_out_of_order_but_join_keeps_collection_order(
    tmp_path: Path,
) -> None:
    from tests.helpers.foreach import foreach_config

    compile_literal_foreach(tmp_path)
    child = tmp_path / "child.md"
    payload, body = workflow_document(child)
    payload["nodes"] = payload["nodes"][:2]
    write_workflow_document(child, payload, body.split("## independent")[0])
    preview = compile_foreach_file(
        tmp_path / "parent.md", foreach_config(max_concurrent_nodes=2)
    )
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)

    async def scenario() -> None:
        second_done = asyncio.Event()
        finished = []

        class ReorderedInvoker(MockAgentInvoker):
            async def invoke(self, *args, **kwargs):
                node_id = kwargs["invocation_context"].node_id
                if node_id == "chunks.i0.analyze":
                    await asyncio.wait_for(second_done.wait(), timeout=3)
                await super().invoke(*args, **kwargs)

        def receive(event):
            if event.event_type == EventType.NODE_FINISHED:
                finished.append(event.context.node_id)
                if event.context.node_id == "chunks.i1.analyze":
                    second_done.set()

        task = asyncio.create_task(
            execute_workflow(
                plan,
                output,
                ReorderedInvoker(),
                preview.secret_context,
                event_sink=receive,
            )
        )
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert (
            finished.index("chunks.i1.analyze")
            < finished.index("chunks.i0.analyze")
            < finished.index("chunks")
        )
        index = json.loads((output.results_dir / "chunks-result.md").read_bytes())
        assert [entry["index"] for entry in index] == [0, 1]

    asyncio.run(scenario())


@pytest.mark.race
def test_cancelled_generated_children_are_awaited(tmp_path: Path) -> None:
    preview = compile_literal_foreach(tmp_path, "[{}]", max_concurrent_nodes=2)
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)

    async def scenario() -> None:
        started = set()
        cleaned = set()
        all_started = asyncio.Event()

        class BlockingInvoker(MockAgentInvoker):
            async def invoke(self, *args, **kwargs):
                del args
                node_id = kwargs["invocation_context"].node_id
                started.add(node_id)
                if len(started) == 2:
                    all_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.add(node_id)

        task = asyncio.create_task(
            execute_workflow(plan, output, BlockingInvoker(), preview.secret_context)
        )
        try:
            await asyncio.wait_for(all_started.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
            assert cleaned == started
            assert len(cleaned) == 2
            assert not (output.results_dir / "chunks-result.md").exists()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
