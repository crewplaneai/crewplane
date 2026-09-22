from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path

import pytest

from crewplane.core import preflight
from crewplane.core.preflight import runner, source
from crewplane.version import SCHEMA_VERSION


def test_preflight_source_import_routes_preserve_callable_and_signature() -> None:
    assert (
        runner.load_workflow_source_for_preflight
        is source.load_workflow_source_for_preflight
    )
    assert (
        preflight.load_workflow_source_for_preflight
        is source.load_workflow_source_for_preflight
    )
    assert runner.PreflightWorkflowSource is source.PreflightWorkflowSource
    signature = inspect.signature(runner.load_workflow_source_for_preflight)
    assert list(signature.parameters) == ["tasks_file", "project_root"]
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in signature.parameters.values()
    )
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in signature.parameters.values()
    )


@pytest.mark.parametrize(
    "loader",
    [
        preflight.load_workflow_source_for_preflight,
        runner.load_workflow_source_for_preflight,
    ],
    ids=["package", "runner"],
)
def test_preflight_source_exports_preserve_provenance_and_errors(
    tmp_path: Path, loader: Callable[[Path, Path], source.PreflightWorkflowSource]
) -> None:
    workflow = tmp_path / "workflow.task.md"
    workflow.write_text(
        f'---\nschema_version: "{SCHEMA_VERSION}"\nname: source-test\nnodes:\n  - id: build\n    mode: sequential\n    providers: [mock]\n---\n\n## build\nBuild it.\n',
        encoding="utf-8",
    )
    result = loader(workflow, tmp_path)
    assert result.root_workflow_path == workflow.resolve()
    assert result.node_source_paths == {"build": workflow.resolve()}
    assert result.workflow_content == workflow.read_text(encoding="utf-8")
    assert [record.path for record in result.referenced_workflows] == [
        workflow.resolve()
    ]
    workflow.write_text("missing frontmatter", encoding="utf-8")
    with pytest.raises(ValueError) as direct:
        source.load_workflow_source_for_preflight(workflow, tmp_path)
    with pytest.raises(type(direct.value), match="frontmatter") as exported:
        loader(workflow, tmp_path)
    assert str(exported.value) == str(direct.value)
