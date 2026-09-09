from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.core.workflow.markdown import (
    parse_workflow_markdown,
    parse_workflow_markdown_text,
)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("<!-- crewplane:unknown -->\n", "unknown crewplane role marker"),
        ("<!-- /crewplane:unknown -->\n", "unknown crewplane role marker"),
        ("<!-- crewplane:Executor -->\n", "malformed crewplane marker"),
        (
            "<!-- crewplane:executor -->\n\n<!-- crewplane:reviewer -->\n",
            "nested crewplane role markers",
        ),
        ("<!-- /crewplane:executor -->\n", "without a matching open marker"),
        (
            "<!-- crewplane:executor -->\n\nContent\n\n<!-- /crewplane:reviewer -->\n",
            "closes role 'reviewer'",
        ),
        (
            "<!-- crewplane:executor -->\n\n<!-- /crewplane:executor -->\n",
            "empty 'executor'",
        ),
        ("<!-- crewplane:executor -->\n\nContent\n", "unclosed 'executor'"),
    ],
)
def test_markdown_parser_rejects_invalid_role_delimiters(
    body: str, message: str
) -> None:
    text = (
        "---\nname: Review\nnodes:\n  - id: review\n    mode: sequential\n    providers: [codex]\n---\n## review\n"
        + body
    )

    with pytest.raises(ValueError, match=message) as caught:
        parse_workflow_markdown_text(Path("review.task.md"), text)

    assert "review.task.md node 'review'" in str(caught.value)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "must start with YAML frontmatter"),
        ("---\nname: Review", "missing the closing frontmatter delimiter"),
        ("---\n[]\n---", "must be a YAML object"),
        ("---\nname: One\nname: Two\n---", "frontmatter is invalid"),
        (
            "---\nname: Review\nnodes:\n  - id: review\n    mode: sequential\n    providers: [' ']\n---\n## review\nText",
            "empty provider name",
        ),
        (
            "---\nname: Review\nnodes:\n  - id: review\n    mode: sequential\n    source: '{{file:context.md}}'\n    providers: [codex]\n---\n## review\nText",
            "source is only valid for input nodes",
        ),
    ],
)
def test_markdown_file_parser_preserves_specific_frontmatter_rejections(
    tmp_path: Path, text: str, message: str
) -> None:
    path = tmp_path / "review.task.md"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match=message) as caught:
        parse_workflow_markdown(path)

    assert str(path) in str(caught.value)
