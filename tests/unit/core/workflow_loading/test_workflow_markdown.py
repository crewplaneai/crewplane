import hashlib
from pathlib import Path

import pytest

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.composition import compose_workflow_markdown
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.markdown import (
    parse_workflow_markdown,
    parse_workflow_markdown_document,
    parse_workflow_markdown_text,
    validate_workflow_markdown_text,
)
from crewplane.core.workflow.models import WorkflowNode, render_prompt_for_role
from crewplane.version import SCHEMA_VERSION


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_file_workflow_readers_preserve_text_prompts_and_source_hashes(
    tmp_path: Path, newline: bytes
) -> None:
    child = tmp_path / "child.task.md"
    root = tmp_path / "root.task.md"
    child_bytes = (
        (
            f'---\nschema_version: "{SCHEMA_VERSION}"\nname: Child\n'
            "nodes:\n  - id: build\n    mode: sequential\n    providers: [alpha]\n"
            "---\n\n## build\n\nLine one with spaces.   \nCafé.\n"
        )
        .encode()
        .replace(b"\n", newline)
    )
    root_bytes = (
        (
            f'---\nschema_version: "{SCHEMA_VERSION}"\nname: Root\n'
            "imports:\n  - path: child.task.md\n    as: child\n"
            "nodes:\n  - id: finish\n    mode: sequential\n    needs: [child.build]\n"
            "    providers: [alpha]\n---\n\n## finish\n\nSummarize.   \n"
        )
        .encode()
        .replace(b"\n", newline)
    )
    child.write_bytes(child_bytes)
    root.write_bytes(root_bytes)
    line_break = newline.decode("ascii")
    expected_child_prompt = line_break.join(
        ["", "Line one with spaces.   ", "Café.", ""]
    )
    expected_root_prompt = line_break.join(["", "Summarize.   ", ""])

    parsed_child = WorkflowNode.model_validate(
        parse_workflow_markdown(child)["nodes"][0]
    )
    loaded = load_tasks_with_sources(root, project_root=tmp_path)
    composed = compose_workflow_markdown(root, project_root=tmp_path)

    assert render_prompt_for_role(parsed_child, PromptSegmentRole.EXECUTOR) == (
        expected_child_prompt
    )
    assert loaded.workflow_content.encode("utf-8") == root_bytes
    assert [
        render_prompt_for_role(node, PromptSegmentRole.EXECUTOR)
        for node in loaded.workflow.nodes
    ] == [expected_child_prompt, expected_root_prompt]
    assert loaded.composed_workflow == composed.workflow_payload
    assert loaded.referenced_workflows == composed.source_records
    assert [(record.path, record.sha256) for record in composed.source_records] == [
        (root, hashlib.sha256(root_bytes).hexdigest()),
        (child, hashlib.sha256(child_bytes).hexdigest()),
    ]
    assert root.read_bytes() == root_bytes
    assert child.read_bytes() == child_bytes


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_legacy_yaml_loading_preserves_source_text_and_hash(
    tmp_path: Path, newline: bytes
) -> None:
    path = tmp_path / "workflow.yaml"
    workflow_bytes = (
        (
            f'schema_version: "{SCHEMA_VERSION}"\nname: Legacy\n'
            "nodes:\n  - id: build\n    mode: sequential\n"
            "    providers: [{provider: alpha}]\n    prompt_segments:\n"
            '      - role: shared\n        content: "Café.   \\nDone.\\n"\n'
            "# Trailing comment spaces.   \n"
        )
        .encode()
        .replace(b"\n", newline)
    )
    path.write_bytes(workflow_bytes)

    loaded = load_tasks_with_sources(path)

    assert loaded.workflow_content.encode("utf-8") == workflow_bytes
    assert render_prompt_for_role(
        loaded.workflow.nodes[0], PromptSegmentRole.EXECUTOR
    ) == ("Café.   \nDone.\n")
    assert [(record.path, record.sha256) for record in loaded.referenced_workflows] == [
        (path, hashlib.sha256(workflow_bytes).hexdigest())
    ]
    assert path.read_bytes() == workflow_bytes


def test_file_workflow_readers_preserve_missing_file_errors(tmp_path: Path) -> None:
    path = tmp_path / "missing.task.md"

    with pytest.raises(FileNotFoundError) as parsed_error:
        parse_workflow_markdown(path)
    with pytest.raises(FileNotFoundError) as loaded_error:
        load_tasks_with_sources(path, project_root=tmp_path)
    with pytest.raises(ValueError) as composed_error:
        compose_workflow_markdown(path, project_root=tmp_path)

    assert parsed_error.value.filename == str(path)
    assert loaded_error.value.filename == str(path)
    assert str(composed_error.value) == f"Workflow file does not exist: {path}"


@pytest.mark.parametrize("suffix", [".task.md", ".yaml"])
def test_file_workflow_readers_reject_invalid_utf8(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"workflow{suffix}"
    path.write_bytes(b"invalid UTF-8: \xff\r\n")

    with pytest.raises(UnicodeDecodeError):
        load_tasks_with_sources(path, project_root=tmp_path)
    if suffix == ".task.md":
        with pytest.raises(UnicodeDecodeError):
            parse_workflow_markdown(path)
        with pytest.raises(UnicodeDecodeError):
            compose_workflow_markdown(path, project_root=tmp_path)
    assert path.read_bytes() == b"invalid UTF-8: \xff\r\n"


@pytest.mark.parametrize(
    ("needs_field", "expected"),
    [
        ("", []),
        ("    needs: []\n", []),
        ('    needs: [" plan.plan.final "]\n', ["plan.plan.final"]),
    ],
)
def test_markdown_parser_preserves_import_prerequisites(
    tmp_path: Path, needs_field: str, expected: list[str]
) -> None:
    payload = parse_workflow_markdown_text(
        tmp_path / "root.task.md",
        "---\nname: Root\nimports:\n  - path: child.task.md\n    as: child\n"
        f"{needs_field}nodes: []\n---\n",
    )

    assert payload["imports"][0]["needs"] == expected


def test_crlf_markdown_preserves_role_segment_boundaries(tmp_path: Path) -> None:
    workflow_path = tmp_path / "workflow.task.md"
    markdown_text = "\r\n".join(
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: CRLF Workflow",
            "nodes:",
            "  - id: build",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## build",
            "",
            "Shared line",
            "<!-- crewplane:reviewer -->",
            "Reviewer line",
            "<!-- /crewplane:reviewer -->",
            "Trailing shared line",
        ]
    )

    summary = validate_workflow_markdown_text(workflow_path, markdown_text)
    payload = parse_workflow_markdown_text(workflow_path, markdown_text)

    assert summary.nodes_defined == 1
    assert summary.node_sections_found == 1
    segments = payload["nodes"][0]["prompt_segments"]
    assert [segment["role"] for segment in segments] == [
        "shared",
        "reviewer",
        "shared",
    ]
    assert "Shared line" in segments[0]["content"]
    assert "Reviewer line" in segments[1]["content"]


def test_markdown_parser_preserves_node_and_prompt_source_spans(
    tmp_path: Path,
) -> None:
    workflow_path = tmp_path / "workflow.task.md"
    markdown_text = "\n".join(
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Span Workflow",
            "nodes:",
            "  - id: build",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## build",
            "",
            "Shared {{file:context.md}}",
            "<!-- crewplane:reviewer -->",
            "Reviewer {{build.output}}",
            "<!-- /crewplane:reviewer -->",
            "Trailing shared",
        ]
    )

    document = parse_workflow_markdown_document(workflow_path, markdown_text)

    assert document.node_source_spans["build"] == {
        "start_line": 9,
        "end_line": 16,
    }
    assert document.prompt_segment_spans["build"] == [
        {"start_line": 10, "end_line": 12},
        {"start_line": 13, "end_line": 14},
        {"start_line": 15, "end_line": 16},
    ]
