from pathlib import Path

import pytest
import yaml

from crewplane.core.workflow.markdown import parse_workflow_markdown_text


def declaration(**overrides: object) -> dict[str, object]:
    return {
        "id": "group",
        "mode": "foreach",
        "workflow": "child.md",
        "item_input": "item",
        "items": [],
        "max_items": 3,
        **overrides,
    }


def parse(
    node: dict[str, object], section: str = "## group\n<!-- empty -->\n"
) -> dict[str, object]:
    return parse_workflow_markdown_text(
        Path("parent.md"),
        "---\n"
        + yaml.safe_dump({"name": "Parent", "nodes": [node]})
        + "---\n"
        + section,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("providers", []),
        ("findings", False),
        ("worktree", "project"),
        ("source", ""),
        ("depth", 1),
        ("audit_rounds", 1),
        ("review_starts_with", "executor"),
        ("continue_on_failure", False),
        ("failure_threshold", 0),
        ("prompt_segments", []),
        ("token_budget", {}),
    ],
)
def test_foreach_rejects_explicit_provider_and_policy_fields(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match="cannot declare"):
        parse(declaration(**{field: value}))


@pytest.mark.parametrize("limit", [True, False, 0, -1, "3", 3.0, None])
def test_foreach_requires_strict_positive_max_items(limit: object) -> None:
    with pytest.raises(ValueError, match="strict positive"):
        parse(declaration(max_items=limit))


@pytest.mark.parametrize(
    "items",
    [
        "{{a.output_path}}",
        "{{a.output}}{{b.output}}",
        "./items.json",
        "*.json",
        "$(list)",
        "{{item.id}}",
    ],
)
def test_foreach_collection_has_no_expressions_or_discovery(items: str) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        parse(declaration(items=items))


@pytest.mark.parametrize(
    "section", ["", "## group\nDo work.", "## group\n<!-- executor -->\nPrompt"]
)
def test_foreach_requires_empty_section(section: str) -> None:
    with pytest.raises(ValueError):
        parse(declaration(), section)


@pytest.mark.parametrize(
    "value",
    [
        '[{1: first, "1": second}]',
        "[{x: .nan}]",
        "[{x: .inf}]",
        "[{x: !!binary YQ==}]",
        "[{x: 2026-01-01}]",
        "[{x: one, x: two}]",
        "&cycle [{self: *cycle}]",
    ],
)
def test_yaml_collections_are_validated_before_coercion(value: str) -> None:
    text = (
        "---\nname: Parent\nnodes:\n  - {id: group, mode: foreach, workflow: child.md, item_input: item, max_items: 2, items: "
        + value
        + "}\n---\n## group\n"
    )
    with pytest.raises(ValueError):
        parse_workflow_markdown_text(Path("parent.md"), text)


def test_comments_empty_and_repeated_objects_preserve_item_data() -> None:
    items = [{"name": "{{file:secret}}"}, {"name": "{{file:secret}}"}]
    payload = parse(declaration(items=items))
    assert payload["nodes"][0]["items"] == items


def test_normalized_binding_names_and_string_parameters_are_strict() -> None:
    with pytest.raises(ValueError, match="duplicate normalized"):
        parse(declaration(inputs={" shared": "a", "shared ": "b"}))
    with pytest.raises(ValueError, match="string values"):
        parse(declaration(**{"with": {"count": 2}}))
