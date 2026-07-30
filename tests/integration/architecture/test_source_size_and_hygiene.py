from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

import pytest

from tests.integration.architecture.static_checks import (
    text_rule_files,
    walk_ast,
)


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
