from pathlib import Path

import pytest

from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.markdown import parse_workflow_markdown_document


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_invalid_provider_role_has_source_location(newline: str) -> None:
    markdown = newline.join(
        [
            "---",
            "name: Design",
            "",
            "nodes:",
            "  - id: first",
            "    mode: sequential",
            "    providers: [{provider: codex, role: executor}]",
            "  - id: design.iteration",
            "    mode: sequential",
            "    providers:",
            "      - provider: codex",
            "        role: executors",
            "---",
        ]
    )

    with pytest.raises(ValueError) as caught:
        parse_workflow_markdown_document(Path("design.task.md"), markdown)

    assert str(caught.value) == (
        "design.task.md:12:15\n"
        "  Node 'design.iteration', provider #1 ('codex'), role:\n"
        "    Invalid value 'executors'.\n"
        "    Allowed values: executor, reviewer."
    )


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ("role: 42", "Invalid value 42.\n    Allowed values: executor, reviewer."),
        ("role: []", "Invalid value <list>.\n    Allowed values: executor, reviewer."),
        ("role: Reviewer", "Values must be lower-case."),
        ("model: []", "model:\n    Input should be a valid string"),
        ("rol: reviewer", "rol:\n    Extra inputs are not permitted"),
    ],
)
def test_provider_errors_do_not_expose_union_details(
    fields: str, expected: str
) -> None:
    markdown = (
        "---\nname: Design\nnodes:\n"
        "  - id: design\n    mode: sequential\n"
        f"    providers: [{{provider: codex, {fields}}}]\n---\n"
    )

    with pytest.raises(ValueError) as caught:
        parse_workflow_markdown_document(Path("design.task.md"), markdown)

    message = str(caught.value)
    assert message.startswith("design.task.md:6:")
    assert expected in message
    assert message.count("design.task.md:") == 1
    assert "ProviderSpec" not in message
    assert "WorkflowFrontmatter" not in message
    assert "pydantic.dev" not in message
    assert "input_value" not in message


def test_validation_preserves_independent_errors() -> None:
    markdown = (
        "---\nname: Design\nnodes:\n"
        "  - id: design\n    mode: serial\n    review_starts_with: both\n"
        "    providers: [{provider: codex, role: executors, model: []}]\n---\n"
    )

    with pytest.raises(ValueError) as caught:
        parse_workflow_markdown_document(Path("design.task.md"), markdown)

    message = str(caught.value)
    assert message.count("design.task.md:") == 4
    assert len(message.split("\n\n")) == 4
    assert (
        "mode:\n    Invalid value 'serial'.\n    Allowed values: parallel, sequential, input, foreach."
        in message
    )
    assert (
        "review_starts_with:\n    Invalid value 'both'.\n    Allowed values: executor, reviewer."
        in message
    )
    assert (
        "role:\n    Invalid value 'executors'.\n    Allowed values: executor, reviewer."
        in message
    )
    assert "model:\n    Input should be a valid string" in message


def test_missing_provider_field_points_to_containing_object() -> None:
    markdown = (
        "---\nname: Design\nnodes:\n  - id: design\n    mode: sequential\n"
        "    providers:\n      - role: executor\n---\n"
    )

    with pytest.raises(ValueError) as caught:
        parse_workflow_markdown_document(Path("design.task.md"), markdown)

    assert str(caught.value) == (
        "design.task.md:7:9\n  Node 'design', provider #1, provider:\n    Field required"
    )


@pytest.mark.parametrize("provider", ["42", "null", "[]"])
def test_invalid_provider_type_has_one_actionable_error(provider: str) -> None:
    markdown = (
        "---\nname: Design\nnodes:\n  - id: design\n    mode: sequential\n"
        f"    providers: [{provider}]\n---\n"
    )

    with pytest.raises(ValueError) as caught:
        parse_workflow_markdown_document(Path("design.task.md"), markdown)

    assert str(caught.value) == (
        "design.task.md:6:17\n"
        "  Node 'design', provider #1:\n    Expected a provider name (string) "
        "or a provider object."
    )


def test_imported_validation_error_identifies_child_file(tmp_path: Path) -> None:
    child = tmp_path / "child.task.md"
    child.write_text(
        "---\nname: Child\nnodes:\n  - id: child\n    mode: sequential\n"
        "    providers: [{provider: codex, role: executors}]\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    parent = tmp_path / "parent.task.md"
    parent.write_text(
        "---\nname: Parent\nnodes: []\n"
        "imports: [{path: child.task.md, as: child}]\n---\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError) as caught:
        load_tasks_with_sources(parent, project_root=tmp_path)

    assert str(caught.value).startswith(f"{child}:6:41\n")
    assert "Invalid value 'executors'" in str(caught.value)


@pytest.mark.parametrize("provider", ["codex", "{provider: codex, role: executor}"])
def test_valid_provider_syntax_is_unchanged(provider: str) -> None:
    markdown = (
        "---\nname: Design\nnodes:\n  - id: design\n    mode: sequential\n"
        f"    providers: [{provider}]\n---\n\n## design\nDesign something.\n"
    )

    parsed = parse_workflow_markdown_document(Path("design.task.md"), markdown)

    assert parsed.frontmatter.nodes[0].id == "design"


@pytest.mark.parametrize(
    ("nodes", "expected"),
    [
        ("[{id: design}]", "Node 'design', mode:\n    Field required"),
        ("[42]", "Node #1:\n    Input should be a YAML object"),
        (
            "[{id: design, mode: sequential, token_budget: 42}]",
            "Node 'design', token_budget:\n    Input should be a YAML object",
        ),
    ],
)
def test_other_schema_failures_keep_actionable_messages(
    nodes: str, expected: str
) -> None:
    markdown = f"---\nname: Design\nnodes: {nodes}\n---\n"

    with pytest.raises(ValueError) as caught:
        parse_workflow_markdown_document(Path("design.task.md"), markdown)

    assert str(caught.value).startswith("design.task.md:3:")
    assert expected in str(caught.value)
