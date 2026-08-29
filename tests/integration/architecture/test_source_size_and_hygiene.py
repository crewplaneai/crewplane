from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

import pytest

from tests.integration.architecture.static_checks import (
    SRC_ROOT,
    text_rule_files,
    walk_ast,
)

MAX_CLAUDE_JSON_MODULE_LINES = 500
CLAUDE_JSON_MODULES = tuple(
    SRC_ROOT / "crewplane" / "adapters" / "invokers" / "cli_invoker" / filename
    for filename in ("claude_json.py", "claude_json_parser.py", "json_number.py")
)


@pytest.mark.parametrize("module", CLAUDE_JSON_MODULES, ids=lambda path: path.name)
def test_claude_json_modules_remain_reviewable(module: Path) -> None:
    line_count = len(module.read_text(encoding="utf-8").splitlines())

    assert line_count <= MAX_CLAUDE_JSON_MODULE_LINES


def test_ast_walker_does_not_depend_on_mutable_stdlib_walk_helpers(
    monkeypatch,
) -> None:
    monkeypatch.setattr(ast, "iter_child_nodes", deque())
    module = ast.parse("value = helper(1)\n")

    assert any(isinstance(node, ast.Call) for node in walk_ast(module))


def test_text_rule_files_rejects_missing_paths(tmp_path: Path) -> None:
    missing_path = tmp_path / "required.md"

    with pytest.raises(FileNotFoundError, match="Text rule path does not exist"):
        text_rule_files((missing_path,))
