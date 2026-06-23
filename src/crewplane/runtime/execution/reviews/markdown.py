from __future__ import annotations

from dataclasses import dataclass

from markdown_it import MarkdownIt


@dataclass(frozen=True)
class ReviewHeading:
    section: str | None
    start_line: int
    content_start_line: int


_COMMONMARK_PARSER = MarkdownIt("commonmark")
_SECTION_BY_HEADING = {
    "major issues": "major_issues",
    "minor issues": "minor_issues",
    "nitpicks": "nitpicks",
}


def code_block_lines(output: str) -> set[int]:
    ignored_lines: set[int] = set()
    for token in _COMMONMARK_PARSER.parse(output):
        if token.type not in {"fence", "code_block"} or token.map is None:
            continue
        ignored_lines.update(range(token.map[0], token.map[1]))
    return ignored_lines


def collect_root_h2_review_headings(
    output: str,
    before_line: int | None = None,
) -> list[ReviewHeading]:
    headings: list[ReviewHeading] = []
    tokens = _COMMONMARK_PARSER.parse(output)
    for index, token in enumerate(tokens):
        if not is_root_atx_h2(token):
            continue
        if token.map is None or index + 1 >= len(tokens):
            continue
        if tokens[index + 1].type != "inline":
            continue
        if before_line is not None and token.map[0] >= before_line:
            continue
        section = review_section_for_heading(tokens[index + 1].content)
        headings.append(
            ReviewHeading(
                section=section,
                start_line=token.map[0],
                content_start_line=token.map[1],
            )
        )
    return headings


def is_root_atx_h2(token: object) -> bool:
    return (
        getattr(token, "type", None) == "heading_open"
        and getattr(token, "tag", None) == "h2"
        and getattr(token, "level", None) == 0
        and getattr(token, "markup", None) == "##"
    )


def review_section_for_heading(heading_text: str) -> str | None:
    return _SECTION_BY_HEADING.get(heading_text.strip().casefold())
