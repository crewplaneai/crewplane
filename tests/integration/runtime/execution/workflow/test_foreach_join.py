import asyncio
import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.naming import node_state_relative_path
from crewplane.core.execution_state import NodeState
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.runtime.execution import execute_workflow
from crewplane.runtime.execution.workflow import join
from crewplane.runtime.execution.workflow.execution_session import (
    WorkflowExecutionSession,
    initialize_workflow_execution,
)
from tests.helpers.foreach import (
    compile_foreach_file,
    compile_literal_foreach,
    freeze_foreach,
    workflow_document,
    write_workflow_document,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
)


@pytest.fixture
def join_ready(
    tmp_path: Path,
) -> Iterator[tuple[WorkflowExecutionSession, OutputManager]]:
    compile_literal_foreach(tmp_path, "[{x: 2}, {x: 1}]")
    child = tmp_path / "child.md"
    payload, body = workflow_document(child)
    payload["nodes"][1]["findings"] = True
    payload["nodes"].append(
        {
            "id": "finish",
            "mode": "parallel",
            "needs": ["analyze"],
            "providers": ["alpha"],
            "findings": True,
        }
    )
    write_workflow_document(child, payload, body + "\n## finish\nFinish analysis.")
    preview = compile_foreach_file(tmp_path / "parent.md")
    output = OutputManager("foreach", base_dir=tmp_path)
    plan = freeze_foreach(preview, output)
    invoker = MockAgentInvoker(outputs=["<!-- findings -->done<!-- /findings -->"] * 6)
    effective = asyncio.run(
        execute_workflow(plan, output, invoker, preview.secret_context)
    )
    (output.results_dir / "chunks-result.md").unlink()
    (output.stages_dir / node_state_relative_path("chunks")).unlink()
    session = initialize_workflow_execution(
        effective, output, preview.secret_context, None, None, True, None, ()
    )
    try:
        yield session, output
    finally:
        session.runtime_context.runtime_publications.close()


def test_join_selects_ordered_terminals_and_registers_recoverable_success(
    join_ready: tuple[WorkflowExecutionSession, OutputManager],
) -> None:
    session, output = join_ready
    statuses = session.state.statuses.copy()
    assert join.join_group(session, "chunks", output) is None
    result = output.results_dir / "chunks-result.md"
    entries = json.loads(result.read_bytes())
    assert [entry["index"] for entry in entries] == [0, 1]
    assert [entry["item"] for entry in entries] == [{"x": 2}, {"x": 1}]
    for index, entry in enumerate(entries):
        assert [node["local_node_id"] for node in entry["results"]] == [
            "independent",
            "finish",
        ]
        for node in entry["results"]:
            assert node["node_id"] == f"chunks.i{index}.{node['local_node_id']}"
            kinds = (
                {"output", "findings"}
                if node["local_node_id"] == "finish"
                else {"output"}
            )
            assert set(node) == {"local_node_id", "node_id"} | kinds
            for kind in kinds:
                artifact = node[kind]
                path = output.results_dir / artifact["path"]
                assert file_size_and_sha256(path) == (
                    artifact["size_bytes"],
                    artifact["sha256"],
                )
    state_path = output.stages_dir / node_state_relative_path("chunks")
    state = NodeState.model_validate_json(state_path.read_bytes())
    assert state.node_id == "chunks" and state.status == "succeeded"
    assert state.workflow_signature == session.runtime_context.plan.workflow_signature
    assert state.workflow_identity == session.workflow_identity
    assert len(state.artifacts) == 1
    descriptor = state.artifacts[0]
    assert descriptor.kind == "output" and descriptor.relative_path == result.name
    assert (descriptor.size_bytes, descriptor.sha256) == file_size_and_sha256(result)
    publications = session.runtime_context.runtime_publications
    signatures, _ = publications.snapshot()
    assert set(signatures) == {result, state_path}
    for path, signature in signatures.items():
        assert signature == file_size_and_sha256(path)
        recovered = io.BytesIO()
        assert publications.copy_recovery_payload_to(path, recovered)
        assert recovered.getvalue() == path.read_bytes()
    assert session.state.statuses == statuses


@pytest.mark.parametrize(
    "damage",
    [
        "collection",
        "item_provenance",
        "nonterminal_output",
        "nonterminal_findings",
        "terminal_output",
        "terminal_findings",
        "size",
    ],
)
def test_join_verifies_all_children_before_publication(
    join_ready: tuple[WorkflowExecutionSession, OutputManager],
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    session, output = join_ready
    match damage:
        case "collection":
            collection = session.runtime_context.plan.expansions["chunks"].collection
            (output.stages_dir / collection.relative_path).write_bytes(b"[]")
        case "item_provenance":
            path = output.stages_dir / node_state_relative_path("chunks.i1.item")
            state = json.loads(path.read_bytes())
            state["expansion"]["digest"] = "f" * 64
            path.write_text(json.dumps(state), encoding="utf-8", newline="\n")
        case "size":
            monkeypatch.setattr(join, "MAX_INDEX_SIZE", 0)
        case _:
            local = "analyze" if damage.startswith("nonterminal") else "finish"
            kind = damage.rsplit("_", 1)[1]
            suffix = "result" if kind == "output" else "findings"
            (output.results_dir / f"chunks.i1.{local}-{suffix}.md").write_bytes(
                b"changed"
            )
    with pytest.raises(ValueError):
        join.join_group(session, "chunks", output)
    assert not (output.results_dir / "chunks-result.md").exists()
    assert not (output.stages_dir / node_state_relative_path("chunks")).exists()
    assert session.runtime_context.runtime_publications.snapshot()[0] == {}


def test_join_propagates_state_write_failure_and_retains_published_result(
    join_ready: tuple[WorkflowExecutionSession, OutputManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, output = join_ready
    error = OSError("success state unavailable")

    def fail_state(
        node_state: NodeState,  # noqa: ARG001 - Required artifact writer parameter.
    ) -> Path:
        raise error

    monkeypatch.setattr(output, "write_node_success_state", fail_state)
    with pytest.raises(OSError, match="success state unavailable") as caught:
        join.join_group(session, "chunks", output)
    assert caught.value is error
    result = output.results_dir / "chunks-result.md"
    assert len(json.loads(result.read_bytes())) == 2
    assert not (output.stages_dir / node_state_relative_path("chunks")).exists()
    publications = session.runtime_context.runtime_publications
    assert publications.snapshot()[0] == {result: file_size_and_sha256(result)}
    recovered = io.BytesIO()
    assert publications.copy_recovery_payload_to(result, recovered)
    assert recovered.getvalue() == result.read_bytes()
