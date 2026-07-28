from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

import pytest

from tests.integration.architecture.static_checks import (
    REPO_ROOT,
    SRC_ROOT,
    ForbiddenTextRule,
    find_forbidden_text,
    physical_line_count,
    python_files,
    text_rule_files,
    walk_ast,
)

PRODUCTION_LINE_LIMIT = 500


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


def test_all_production_modules_stay_under_line_limit() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {physical_line_count(path)}"
        for path in python_files(SRC_ROOT / "crewplane")
        if physical_line_count(path) > PRODUCTION_LINE_LIMIT
    ]
    assert offenders == []


def test_source_does_not_suppress_unused_arguments() -> None:
    rule = ForbiddenTextRule(
        name="production source does not suppress unused arguments",
        paths=python_files(SRC_ROOT / "crewplane"),
        forbidden_terms=frozenset({"noqa: ARG002"}),
    )
    assert find_forbidden_text(rule) == []
