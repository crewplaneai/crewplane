from pathlib import Path

import pytest
from pydantic import ValidationError

from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.markdown import parse_workflow_markdown_document
from crewplane.core.workflow.markdown.models import WorkflowImportConfig
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    workflow_payload_dict,
)
from crewplane.version import SCHEMA_VERSION


def write_workflow(path: Path, count: str | None) -> None:
    field = f"repeat_force_run_count: {count}\n" if count is not None else ""
    content = f'name: Repeat\nschema_version: "{SCHEMA_VERSION}"\n{field}nodes: []\n'
    if path.suffix == ".md":
        content = f"---\n{content}---\n"
    path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("suffix", [".task.md", ".yaml"])
@pytest.mark.parametrize("count", [None, 1, 3, 100])
def test_repeat_count_is_preserved_only_when_authored(
    tmp_path: Path, suffix: str, count: int | None
) -> None:
    path = tmp_path / f"workflow{suffix}"
    write_workflow(path, str(count) if count is not None else None)
    loaded = load_tasks_with_sources(path, tmp_path)
    source = load_workflow_source_for_preflight(path, tmp_path)

    assert loaded.workflow.repeat_force_run_count == count
    payloads = [loaded.composed_workflow, workflow_payload_dict(loaded.workflow)]
    payloads.append(source.composed_workflow)
    if suffix == ".task.md":
        payloads.append(
            parse_workflow_markdown_document(
                path, path.read_text(encoding="utf-8")
            ).payload
        )
    for payload in payloads:
        assert payload.get("repeat_force_run_count") == count
        assert ("repeat_force_run_count" in payload) == (count is not None)


@pytest.mark.parametrize("suffix", [".task.md", ".yaml"])
@pytest.mark.parametrize(
    "count",
    ["null", "~", "", "true", "false", '"3"', "1.0", "0", "-1", "[]", "{}", "nope"],
)
def test_invalid_repeat_count_fails_loading(
    tmp_path: Path, suffix: str, count: str
) -> None:
    path = tmp_path / f"workflow{suffix}"
    write_workflow(path, count)
    with pytest.raises(ValueError, match="repeat_force_run_count"):
        load_tasks_with_sources(path, tmp_path)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (WorkflowNode, {"id": "a", "mode": "sequential"}),
        (ProviderSpec, {"provider": "alpha"}),
        (WorkflowImportConfig, {"path": "child.task.md", "as": "child"}),
        (Config, {"version": SCHEMA_VERSION, "agents": {}}),
        (Settings, {}),
        (AgentConfig, {"cli_cmd": ["echo"]}),
    ],
)
def test_repeat_count_is_not_valid_outside_workflow(model, payload) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError, match="repeat_force_run_count"):
        model.model_validate({**payload, "repeat_force_run_count": 3})
