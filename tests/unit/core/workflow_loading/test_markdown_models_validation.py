import pytest
from pydantic import ValidationError

from crewplane.core.workflow.markdown.models import (
    WorkflowFrontmatter,
    WorkflowImportConfig,
    WorkflowNodeConfig,
)


def test_node_config_normalizes_worktree_selector() -> None:
    node = WorkflowNodeConfig.model_validate(
        {
            "id": "build",
            "mode": "parallel",
            "review_starts_with": "reviewer",
            "worktree": "  isolated  ",
        }
    )

    assert node.worktree == "isolated"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        pytest.param(
            {"id": "build", "mode": "parallel", "workspace": {}},
            "node workspace blocks have been removed",
            id="removed-workspace",
        ),
        pytest.param(
            {"id": "build", "mode": "Parallel"},
            "node mode must be lower-case",
            id="invalid-mode",
        ),
        pytest.param(
            {
                "id": "build",
                "mode": "parallel",
                "review_starts_with": "Reviewer",
            },
            "review_starts_with must be lower-case",
            id="invalid-review-start",
        ),
        pytest.param(
            {"id": "build", "mode": "parallel", "worktree": "  "},
            "worktree selector cannot be blank",
            id="blank-worktree",
        ),
    ],
)
def test_node_config_rejects_removed_or_invalid_values(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        WorkflowNodeConfig.model_validate(payload)


def test_import_config_normalizes_alias_parameters_and_inputs() -> None:
    imported = WorkflowImportConfig.model_validate(
        {
            "path": "  workflows/review.task.md  ",
            "as": "  review  ",
            "with": {"  topic  ": "quality"},
            "inputs": {"  source  ": "  build  "},
        }
    )

    assert imported.path == "workflows/review.task.md"
    assert imported.alias == "review"
    assert imported.with_params == {"topic": "quality"}
    assert imported.input_bindings == {"source": "build"}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        pytest.param("path", "  ", "path must be a non-empty string", id="path-blank"),
        pytest.param("as", "  ", "alias must be a non-empty string", id="alias-blank"),
        pytest.param("as", "Upper", "alias is invalid", id="alias-pattern"),
        pytest.param("with", [], "'with' value must be a mapping", id="params-list"),
        pytest.param(
            "with",
            {1: "value"},
            "parameter keys must be strings",
            id="param-key-type",
        ),
        pytest.param(
            "with",
            {" ": "value"},
            "parameter keys must be non-empty strings",
            id="param-key-blank",
        ),
        pytest.param(
            "with",
            {"key": 1},
            "parameter 'key' must be a string",
            id="param-value-type",
        ),
        pytest.param(
            "with",
            {"key": "one", " key ": "two"},
            "Duplicate workflow import parameter key 'key'",
            id="param-duplicate",
        ),
        pytest.param(
            "inputs", [], "'inputs' value must be a mapping", id="inputs-list"
        ),
        pytest.param(
            "inputs",
            {1: "node"},
            "input keys must be strings",
            id="input-key-type",
        ),
        pytest.param(
            "inputs",
            {" ": "node"},
            "input keys must be non-empty strings",
            id="input-key-blank",
        ),
        pytest.param(
            "inputs",
            {"source": 1},
            "input 'source' must be a string",
            id="input-value-type",
        ),
        pytest.param(
            "inputs",
            {"source": "  "},
            "must reference a non-empty node id",
            id="input-value-blank",
        ),
        pytest.param(
            "inputs",
            {"source": "one", " source ": "two"},
            "Duplicate workflow import input key 'source'",
            id="input-duplicate",
        ),
    ],
)
def test_import_config_rejects_invalid_values(
    field: str,
    value: object,
    message: str,
) -> None:
    payload: dict[str, object] = {
        "path": "workflow.task.md",
        "as": "child",
        field: value,
    }

    with pytest.raises(ValidationError, match=message):
        WorkflowImportConfig.model_validate(payload)


def test_import_config_treats_null_mappings_as_empty() -> None:
    imported = WorkflowImportConfig.model_validate(
        {
            "path": "workflow.task.md",
            "as": "child",
            "with": None,
            "inputs": None,
        }
    )

    assert imported.with_params == {}
    assert imported.input_bindings == {}


def test_frontmatter_normalizes_worktrees_and_inputs() -> None:
    frontmatter = WorkflowFrontmatter.model_validate(
        {
            "name": "workflow",
            "nodes": [{"id": "build", "mode": "parallel"}],
            "worktrees": {"  isolated  ": {"kind": "worktree"}},
            "inputs": {"  source  ": "  build  "},
        }
    )

    assert set(frontmatter.worktrees) == {"isolated"}
    assert frontmatter.inputs == {"source": "build"}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        pytest.param(
            "workspace",
            {},
            "workflow workspace blocks have been removed",
            id="removed-workspace",
        ),
        pytest.param(
            "schema_version",
            "0",
            "Unsupported workflow schema version",
            id="schema-version",
        ),
        pytest.param(
            "worktrees",
            {1: {"kind": "worktree"}},
            "worktree names must be strings",
            id="worktree-key-type",
        ),
        pytest.param(
            "worktrees",
            {"name": {"kind": "worktree"}, " name ": {"kind": "worktree"}},
            "Duplicate worktree name 'name'",
            id="worktree-duplicate",
        ),
        pytest.param(
            "inputs", [], "'inputs' value must be a mapping", id="inputs-list"
        ),
        pytest.param(
            "inputs",
            {1: "node"},
            "input keys must be strings",
            id="input-key-type",
        ),
        pytest.param(
            "inputs",
            {" ": "node"},
            "input keys must be non-empty strings",
            id="input-key-blank",
        ),
        pytest.param(
            "inputs",
            {"source": 1},
            "input 'source' must reference a node id string",
            id="input-value-type",
        ),
        pytest.param(
            "inputs",
            {"source": "one", " source ": "two"},
            "Duplicate workflow input key 'source'",
            id="input-duplicate",
        ),
    ],
)
def test_frontmatter_rejects_invalid_values(
    field: str,
    value: object,
    message: str,
) -> None:
    payload: dict[str, object] = {
        "name": "workflow",
        "nodes": [{"id": "build", "mode": "parallel"}],
        field: value,
    }

    with pytest.raises(ValidationError, match=message):
        WorkflowFrontmatter.model_validate(payload)


def test_frontmatter_treats_null_mappings_as_empty() -> None:
    frontmatter = WorkflowFrontmatter.model_validate(
        {
            "name": "workflow",
            "nodes": [{"id": "build", "mode": "parallel"}],
            "worktrees": None,
            "inputs": None,
        }
    )

    assert frontmatter.worktrees == {}
    assert frontmatter.inputs == {}
