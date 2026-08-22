from __future__ import annotations

from pathlib import Path
from typing import assert_type

from crewplane.architecture.contracts import (
    AgentInvoker,
    NodeArtifactRequest,
    artifact_contract_for_node,
)
from crewplane.architecture.ports import ArtifactStorePort, RuntimeComponents
from crewplane.architecture.ports.artifacts import StageFinalizeResult
from crewplane.artifacts import FindingsExtractionError, OutputManager

output = OutputManager("workflow")
assert_type(output, OutputManager)


request = NodeArtifactRequest(
    "build",
    artifact_contract_for_node("build", findings_enabled=True),
)


def consume_store(store: ArtifactStorePort) -> None:
    assert_type(store.create_node_dir(request), Path)
    assert_type(store.get_node_dir(request), Path | None)
    assert_type(store.finalize_node(request), StageFinalizeResult)
    assert_type(store.get_node_output_path(request), Path)
    assert_type(store.get_node_findings_path(request), Path | None)
    assert_type(store.write_node_resume_source(request, {"source": "run-a"}), Path)


def construct_with_store(
    store: ArtifactStorePort,
    invoker: AgentInvoker,
) -> RuntimeComponents:
    components = RuntimeComponents(
        artifact_store=store,
        base_invoker=invoker,
        observers=(),
        suppress_progress_output=False,
    )
    assert_type(components.artifact_store, ArtifactStorePort)
    return components


assert_type(output.create_node_dir(request), Path)
assert_type(output.get_node_output_path(request), Path)
assert_type(output.get_node_findings_path(request), Path | None)
assert_type(output.write_node_resume_source(request, {"source": "run-a"}), Path)

try:
    raise FindingsExtractionError("invalid findings")
except FindingsExtractionError as error:
    assert_type(error, FindingsExtractionError)
