from __future__ import annotations

from collections.abc import Sequence

from .rendering_errors import OnboardingRenderingError


def comment_yaml_block(block: str, indent: int) -> str:
    prefix = " " * indent
    lines = []
    for line in block.splitlines():
        if line == "" or line.startswith(f"{prefix}#"):
            lines.append(line)
        elif line.startswith(prefix):
            lines.append(f"{prefix}# {line[indent:]}")
        else:
            raise OnboardingRenderingError("Default config block indentation changed.")
    return "\n".join(lines)


def uncomment_comment_block(block: str) -> str:
    return uncomment_yaml_comment_block(block, 2)


def uncomment_yaml_comment_block(block: str, indent: int) -> str:
    prefix = " " * indent
    comment_prefix = f"{prefix}# "
    blank_comment = f"{prefix}#"
    lines = []
    for line in block.splitlines():
        if line == blank_comment:
            lines.append("")
        elif line.startswith(comment_prefix):
            lines.append(f"{prefix}{line[len(comment_prefix) :]}")
        else:
            raise OnboardingRenderingError("Default comment block changed.")
    return "\n".join(lines)


def replace_once(text: str, old: str, new: str, description: str) -> str:
    count = text.count(old)
    if count != 1:
        raise OnboardingRenderingError(
            f"Expected one {description} anchor, found {count}."
        )
    return text.replace(old, new, 1)


def mapping_key_line_index(
    lines: Sequence[str],
    key: str,
    indent: int,
    description: str,
) -> int:
    marker = f"{' ' * indent}{key}:"
    indices = [index for index, line in enumerate(lines) if line == marker]
    if len(indices) != 1:
        raise OnboardingRenderingError(
            f"Expected one {description} block, found {len(indices)}."
        )
    return indices[0]


def frontmatter_mapping_key_line_index(
    lines: Sequence[str],
    frontmatter_start: int,
    frontmatter_end: int,
    key: str,
    indent: int,
    description: str,
) -> int:
    marker = f"{' ' * indent}{key}:"
    indices = [
        index
        for index in range(frontmatter_start, frontmatter_end)
        if lines[index] == marker
    ]
    if len(indices) != 1:
        raise OnboardingRenderingError(
            f"Expected one {description} block, found {len(indices)}."
        )
    return indices[0]


def active_mapping_key_line_index(
    lines: Sequence[str],
    parent_index: int,
    key: str,
    description: str,
) -> int:
    parent_indent = leading_spaces(lines[parent_index])
    child_indent = parent_indent + 2
    for index in range(parent_index + 1, len(lines)):
        line = lines[index]
        if line.strip() and leading_spaces(line) <= parent_indent:
            break
        if leading_spaces(line) == child_indent and line.strip().startswith(f"{key}:"):
            return index
    raise OnboardingRenderingError(f"Default config is missing {description}.")


def yaml_child_block_end(
    lines: Sequence[str],
    start_index: int,
    parent_indent: int,
) -> int:
    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        if line.strip() and leading_spaces(line) <= parent_indent:
            return index
    return len(lines)


def leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def line_indent(line: str) -> str:
    return line[: leading_spaces(line)]


def join_lines_like(original: str, lines: Sequence[str]) -> str:
    text = "\n".join(lines)
    if original.endswith("\n"):
        return f"{text}\n"
    return text


__all__ = [
    "active_mapping_key_line_index",
    "comment_yaml_block",
    "frontmatter_mapping_key_line_index",
    "join_lines_like",
    "leading_spaces",
    "line_indent",
    "mapping_key_line_index",
    "replace_once",
    "uncomment_comment_block",
    "uncomment_yaml_comment_block",
    "yaml_child_block_end",
]
