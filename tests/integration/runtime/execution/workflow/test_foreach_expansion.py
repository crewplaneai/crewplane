import asyncio
import io
from pathlib import Path

import pytest

from crewplane.architecture.contracts import EventType, ExecutionStatus
from crewplane.artifacts import OutputManager
from crewplane.artifacts.expansions import reconstruct_expansions
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.runtime.execution.workflow.execution_session import (
    initialize_workflow_execution,
)
from crewplane.runtime.execution.workflow.expansion import expand_group
from tests.helpers.foreach import (
    compile_foreach_file,
    compile_literal_foreach,
    freeze_foreach,
    workflow_document,
    write_workflow_document,
)


@pytest.mark.race
def test_expansion_installs_before_event_and_preserves_scheduler_state(
    tmp_path: Path,
) -> None:
    compile_literal_foreach(tmp_path, "[{}]")
    parent = tmp_path / "parent.md"
    payload, body = workflow_document(parent)
    for node_id, needs in (
        ("complete", []),
        ("active", []),
        ("failed", []),
        ("blocked", ["failed"]),
        ("waiting", ["active"]),
        ("released", ["complete"]),
    ):
        payload["nodes"].append(
            {
                "id": node_id,
                "mode": "parallel",
                "needs": needs,
                "providers": ["alpha"],
            }
        )
        body += f"\n## {node_id}\nIndependent work.\n"
    write_workflow_document(parent, payload, body)
    preview = compile_foreach_file(parent)
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)
    base_bytes = (output.stages_dir / "preflight/execution-plan.json").read_bytes()
    events = []

    def receive(event):
        if event.event_type != EventType.TOPOLOGY_EXPANDED:
            return
        effective = session.runtime_context.plan
        assert "chunks" in effective.expansions
        assert session.nodes_by_id == {node.id: node for node in effective.nodes}
        assert reconstruct_expansions(plan, output.stages_dir).plan == effective
        publications = session.runtime_context.runtime_publications
        signatures, _ = publications.snapshot()
        for path, signature in signatures.items():
            assert signature == file_size_and_sha256(path)
            recovered = io.BytesIO()
            assert publications.copy_recovery_payload_to(path, recovered)
            assert recovered.getvalue() == path.read_bytes()
        assert len(signatures) == 3
        events.append(event)

    session = initialize_workflow_execution(
        plan, output, preview.secret_context, receive, None, True, None, ()
    )
    state = session.state
    nodes = session.nodes_by_id
    running = state.running
    errors = state.node_errors
    state.statuses.update(
        complete=ExecutionStatus.SUCCEEDED,
        active=ExecutionStatus.RUNNING,
        failed=ExecutionStatus.FAILED,
        blocked=ExecutionStatus.BLOCKED,
    )
    prior_statuses = state.statuses.copy()
    error = ValueError("independent failure")
    errors["failed"] = error

    async def scenario() -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def active() -> None:
            started.set()
            await release.wait()

        task = asyncio.create_task(active())
        running["active"] = task
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            assert expand_group(session, "chunks", output) is None
            assert session.state is state
            assert session.nodes_by_id is nodes
            assert state.running is running and running == {"active": task}
            assert not task.done()
            assert state.node_errors is errors and errors == {"failed": error}
            assert all(
                state.statuses[key] == value for key, value in prior_statuses.items()
            )
            assert state.ready == ["chunks.i0.item", "released"]
            assert state.failed_dependencies["blocked"] == {"failed"}
            assert state.remaining_dependencies["waiting"] == 1
            assert state.remaining_dependencies["released"] == 0
            assert state.dependencies_by_node["chunks"] == {
                "chunks.i0.analyze",
                "chunks.i0.independent",
            }
            assert state.dependents["active"] == ["waiting"]
            assert state.node_order["chunks.i0.item"] < state.node_order["chunks"]
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task), timeout=3)

    try:
        asyncio.run(scenario())
        assert len(events) == 1
        payload = events[0].payload
        assert payload.group_id == "chunks" and payload.item_count == 1
        assert [node.id for node in payload.additions] == [
            "chunks.i0.item",
            "chunks.i0.independent",
            "chunks.i0.analyze",
        ]
        assert payload.group_dependencies == (
            "chunks.i0.independent",
            "chunks.i0.analyze",
        )
        assert (
            output.stages_dir / "preflight/execution-plan.json"
        ).read_bytes() == base_bytes
        assert not plan.expansions
    finally:
        session.runtime_context.runtime_publications.close()


def test_expansion_commit_failure_keeps_scheduler_and_partial_publications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = compile_literal_foreach(tmp_path, "[{}]")
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)
    events = []
    session = initialize_workflow_execution(
        plan, output, preview.secret_context, events.append, None, True, None, ()
    )
    events.clear()
    state = session.state
    state_fields = vars(state).copy()
    nodes = session.nodes_by_id.copy()
    error = OSError("commit unavailable")

    def fail_commit(
        path: str,  # noqa: ARG001 - Required artifact writer callback parameter.
        payload: object,  # noqa: ARG001 - Required artifact writer callback parameter.
    ) -> Path:
        raise error

    monkeypatch.setattr(output, "write_preflight_json", fail_commit)
    try:
        with pytest.raises(OSError, match="commit unavailable") as caught:
            expand_group(session, "chunks", output)
        assert caught.value is error
        assert session.state is state and vars(state) == state_fields
        assert session.runtime_context.plan is plan
        assert session.nodes_by_id == nodes
        assert not events
        root = output.stages_dir / "preflight/expansions/chunks"
        assert (root / "items.json").read_bytes() == b"[{}]"
        assert (root / "items/0.json").read_bytes() == b"{}"
        assert not (root / "expansion.json").exists()
        signatures, _ = session.runtime_context.runtime_publications.snapshot()
        assert set(signatures) == {root / "items.json", root / "items/0.json"}
    finally:
        session.runtime_context.runtime_publications.close()
