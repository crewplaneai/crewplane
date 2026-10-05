from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from collections import deque
from pathlib import Path

import pytest

from tests.integration.architecture.static_checks import (
    REPO_ROOT,
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
    module = ast.parse("value = {**helper(1), 'nested': helper(2)}\n")
    expected = list(ast.walk(module))
    monkeypatch.setattr(ast, "iter_child_nodes", deque())

    assert list(walk_ast(module)) == expected


def test_ast_walker_repeated_traversal_under_coverage() -> None:
    script = textwrap.dedent(
        """\
        import ast
        import coverage

        from tests.integration.architecture.static_checks import walk_ast

        modules = [ast.parse("value = helper(1)\\n" * 1000) for _ in range(20)]
        expected = sum(1 for module in modules for _ in ast.walk(module))
        collector = coverage.Coverage(
            data_file=None, source=["crewplane"], branch=True,
        )
        collector.start()
        try:
            for _ in range(100):
                assert sum(1 for module in modules for _ in walk_ast(module)) == expected
        finally:
            collector.stop()
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_text_rule_files_rejects_missing_paths(tmp_path: Path) -> None:
    missing_path = tmp_path / "required.md"

    with pytest.raises(FileNotFoundError, match="Text rule path does not exist"):
        text_rule_files((missing_path,))
