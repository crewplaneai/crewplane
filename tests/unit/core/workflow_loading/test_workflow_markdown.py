from pathlib import Path

from orchestrator_cli.core.workflow_markdown import (
    parse_workflow_markdown_document,
    parse_workflow_markdown_text,
    validate_workflow_markdown_text,
)
from orchestrator_cli.versions import WORKFLOW_SCHEMA_VERSION


def test_crlf_markdown_preserves_role_segment_boundaries(tmp_path: Path) -> None:
    workflow_path = tmp_path / "workflow.task.md"
    markdown_text = "\r\n".join(
        [
            "---",
            f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
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
            "<!-- orchestrator:reviewer -->",
            "Reviewer line",
            "<!-- /orchestrator:reviewer -->",
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
            f'schema_version: "{WORKFLOW_SCHEMA_VERSION}"',
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
            "<!-- orchestrator:reviewer -->",
            "Reviewer {{build.output}}",
            "<!-- /orchestrator:reviewer -->",
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
