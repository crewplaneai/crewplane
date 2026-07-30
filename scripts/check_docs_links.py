from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from re import sub
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION_DIRECTORY_ROOTS = (
    Path(".github"),
    Path("docs"),
    Path("packaging"),
    Path("src/crewplane/example_templates"),
)
ROOT_DOCUMENTATION_NAMES = frozenset(
    {
        "AGENTS.md",
        "CHANGELOG.md",
        "CLAUDE.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "DEVELOPMENT.md",
        "GEMINI.md",
        "GOVERNANCE.md",
        "MAINTAINERS.md",
        "README.md",
        "SECURITY.md",
        "SUPPORT.md",
    }
)


def documentation_files(root: Path = REPOSITORY_ROOT) -> tuple[Path, ...]:
    candidates = [
        path
        for path in root.iterdir()
        if path.is_file() and path.name in ROOT_DOCUMENTATION_NAMES
    ]
    for relative_root in DOCUMENTATION_DIRECTORY_ROOTS:
        docs_root = root / relative_root
        if docs_root.is_dir():
            candidates.extend(docs_root.rglob("*.md"))
    return tuple(sorted(candidates))


def broken_local_links(
    files: Iterable[Path],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> tuple[str, ...]:
    parser = MarkdownIt("commonmark")
    failures: list[str] = []
    anchor_cache: dict[Path, frozenset[str]] = {}
    resolved_repository_root = repository_root.resolve()
    for source in files:
        tokens = parser.parse(source.read_text(encoding="utf-8"))
        for target, line in _link_targets(tokens):
            resolved_target = _resolve_local_target(
                source,
                target,
                repository_root=repository_root,
            )
            if resolved_target is None:
                continue
            resolved_target = resolved_target.resolve(strict=False)
            if not resolved_target.is_relative_to(resolved_repository_root):
                failures.append(
                    f"{source.relative_to(repository_root)}:{line}: "
                    f"local link target escapes the repository: {target!r}"
                )
                continue
            if not resolved_target.exists():
                failures.append(
                    f"{source.relative_to(repository_root)}:{line}: "
                    f"missing local link target {target!r}"
                )
                continue
            fragment = unquote(urlsplit(_normalized_target(target)).fragment)
            if (
                fragment
                and resolved_target.suffix.lower() == ".md"
                and fragment
                not in anchor_cache.setdefault(
                    resolved_target,
                    _markdown_heading_anchors(resolved_target, parser),
                )
            ):
                failures.append(
                    f"{source.relative_to(repository_root)}:{line}: "
                    f"missing local link anchor '#{fragment}' in "
                    f"{resolved_target.relative_to(repository_root)}"
                )
    return tuple(failures)


def _link_targets(
    tokens: Iterable[Token],
    inherited_line: int | None = None,
) -> Iterable[tuple[str, int]]:
    for token in tokens:
        line = (
            token.map[0] + 1
            if token.map is not None
            else inherited_line
            if inherited_line is not None
            else 1
        )
        if token.type == "inline":
            yield from _link_targets(token.children or (), line)
        elif token.type == "link_open":
            target = token.attrGet("href")
            if target:
                yield target, line
        elif token.type == "image":
            target = token.attrGet("src")
            if target:
                yield target, line


def _resolve_local_target(
    source: Path,
    target: str,
    *,
    repository_root: Path,
) -> Path | None:
    normalized_target = _normalized_target(target)
    parsed = urlsplit(normalized_target)
    if parsed.scheme or parsed.netloc or normalized_target.startswith("//"):
        return None
    if not parsed.path:
        return source if parsed.fragment else None
    decoded_path = unquote(parsed.path)
    if decoded_path.startswith("/"):
        return repository_root / decoded_path.lstrip("/")
    return source.parent / decoded_path


def _normalized_target(target: str) -> str:
    normalized = target.strip()
    if normalized.startswith("<") and normalized.endswith(">"):
        return normalized[1:-1]
    return normalized


def _markdown_heading_anchors(
    path: Path,
    parser: MarkdownIt,
) -> frozenset[str]:
    tokens = parser.parse(path.read_text(encoding="utf-8"))
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or index + 1 >= len(tokens):
            continue
        inline = tokens[index + 1]
        if inline.type != "inline":
            continue
        base = _github_heading_slug(_inline_plain_text(inline))
        occurrence = occurrences.get(base, 0)
        occurrences[base] = occurrence + 1
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")
    return frozenset(anchors)


def _inline_plain_text(token: Token) -> str:
    return "".join(
        child.content
        for child in token.children or ()
        if child.type in {"code_inline", "text"}
    )


def _github_heading_slug(value: str) -> str:
    retained = "".join(
        character
        for character in value.strip().lower()
        if character.isalnum()
        or character == "_"
        or character == "-"
        or character.isspace()
    )
    return sub(r"\s+", "-", retained)


def main() -> int:
    failures = broken_local_links(documentation_files())
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Checked local links in {len(documentation_files())} Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
