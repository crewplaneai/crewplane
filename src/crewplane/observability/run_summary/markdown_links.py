from __future__ import annotations

import os
from html import escape
from pathlib import Path
from urllib.parse import quote

from .models import RunSummary


def markdown_text(value: object) -> str:
    text = escape(" ".join(str(value).splitlines()), quote=False)
    for character in ("\\", "`", "[", "]", "*", "_", "|"):
        text = text.replace(character, "\\" + character)
    return text


def artifact_link(
    summary: RunSummary,
    path: Path | str,
    label: str,
    run_relative: bool = False,
) -> str:
    target = Path(path)
    base = summary.summary_path.parent
    if not base.is_absolute():
        return markdown_text(path)
    if not target.is_absolute():
        if not run_relative:
            return markdown_text(path)
        target = base.parent / target
    try:
        relative = Path(os.path.relpath(target, base)).as_posix()
    except ValueError:
        return markdown_text(path)
    return f"[{markdown_text(label)}]({quote(relative, safe='/.-_~')})"
