from pathlib import Path

import pytest

from crewplane.core.workflow.markdown import (
    parse_workflow_markdown_document,
    parse_workflow_markdown_text,
    validate_workflow_markdown_text,
)


@pytest.mark.parametrize(
    ("provider", "prompt", "message"),
    [
        (
            " ",
            "<!-- crewplane:unknown -->\n",
            "review.task.md contains an empty provider name in frontmatter.",
        ),
        (
            "alpha",
            "<!-- crewplane:unknown -->\n",
            "review.task.md node 'review' declares unknown crewplane role marker "
            "'unknown'. Allowed roles: executor, reviewer.",
        ),
        (
            "alpha",
            "Review this.\n",
            "review.task.md node 'review' declares source, but source is only "
            "valid for input nodes.",
        ),
    ],
)
def test_node_payload_validation_preserves_error_precedence(
    provider: str, prompt: str, message: str
) -> None:
    text = (
        "---\nname: Review\nnodes:\n"
        "  - id: review\n    mode: sequential\n"
        "    source: '{{file:context.md}}'\n"
        f"    providers: ['{provider}']\n---\n## review\n{prompt}"
    )

    with pytest.raises(ValueError) as caught:
        parse_workflow_markdown_text(Path("review.task.md"), text)

    assert str(caught.value) == message


def test_payload_and_source_maps_follow_frontmatter_order_across_node_modes() -> None:
    path = Path("workflow.task.md")
    text = (
        "---\nname: Workflow\nnodes:\n"
        "  - id: source\n    mode: input\n    source: '{{file:input.md}}'\n"
        "  - id: review\n    mode: sequential\n"
        "    providers: [' alpha ']\n    needs: [source]\n"
        "  - id: group\n    mode: foreach\n    workflow: child.md\n"
        "    item_input: item\n    items: []\n    max_items: 2\n"
        "    needs: [review]\n---\n"
        "## group\n<!-- comment\nonly -->\n"
        "## review\nReview this.\n"
    )

    document = parse_workflow_markdown_document(path, text)
    summary = validate_workflow_markdown_text(path, text)

    source, review, group = document.payload["nodes"]
    assert [node["id"] for node in document.payload["nodes"]] == [
        "source",
        "review",
        "group",
    ]
    assert source["source"] == "{{file:input.md}}"
    assert "prompt_segments" not in source
    assert review["providers"] == [{"provider": "alpha"}]
    assert review["prompt_segments"] == [
        {"role": "shared", "content": "Review this.\n"}
    ]
    assert "prompt_segments" not in group
    assert list(document.node_source_spans) == ["review", "group"]
    assert document.node_source_spans == {
        "review": {"start_line": 21, "end_line": 23},
        "group": {"start_line": 18, "end_line": 21},
    }
    assert list(document.prompt_segment_spans) == ["source", "review", "group"]
    assert document.prompt_segment_spans == {
        "source": [],
        "review": [{"start_line": 22, "end_line": 23}],
        "group": [],
    }
    assert summary.nodes_defined == 3
    assert summary.node_sections_found == 2
    assert summary.edges_defined == 2
