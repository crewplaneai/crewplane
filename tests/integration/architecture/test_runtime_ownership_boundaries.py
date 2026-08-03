from __future__ import annotations

import ast

from tests.integration.architecture.static_checks import (
    SRC_ROOT,
    call_name,
    expression_chain,
    offender,
    parse_python,
    python_files,
    walk_ast,
)


def test_runtime_and_cli_do_not_own_module_level_console_singletons() -> None:
    offenders: list[str] = []
    checked_roots = (
        SRC_ROOT / "crewplane" / "runtime",
        SRC_ROOT / "crewplane" / "cli",
    )
    for root in checked_roots:
        for path in python_files(root):
            for node in parse_python(path).body:
                value: ast.expr | None
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                else:
                    continue
                if isinstance(value, ast.Call) and call_name(value.func) == "Console":
                    offenders.append(offender(path, node.lineno))
    assert offenders == []


def test_runtime_code_uses_event_builders_instead_of_direct_construction() -> None:
    offenders: list[str] = []
    for path in python_files(SRC_ROOT / "crewplane" / "runtime"):
        for node in walk_ast(parse_python(path)):
            if isinstance(node, ast.Call) and call_name(node.func) == "ExecutionEvent":
                offenders.append(offender(path, node.lineno))
    assert offenders == []


def test_runtime_execution_does_not_consume_top_level_config() -> None:
    offenders = [
        offender(path, node.lineno, alias.name)
        for path in python_files(SRC_ROOT / "crewplane" / "runtime" / "execution")
        for node in walk_ast(parse_python(path))
        if isinstance(node, ast.ImportFrom) and node.module == "crewplane.core.config"
        for alias in node.names
        if alias.name == "Config"
    ]
    assert offenders == []


def test_runtime_does_not_infer_provider_behavior_from_executable_names() -> None:
    forbidden_names = {
        "AUTO_QUOTA_PARSER_PROVIDER_BY_EXECUTABLE",
        "parser_resolution",
    }
    offenders: list[str] = []
    for path in python_files(SRC_ROOT / "crewplane" / "runtime" / "agent"):
        module = parse_python(path)
        for node in walk_ast(module):
            referenced_name: str | None = None
            if isinstance(node, ast.Name):
                referenced_name = node.id
            elif isinstance(node, ast.Attribute):
                referenced_name = node.attr
            elif isinstance(node, ast.alias):
                referenced_name = node.name.rsplit(".", 1)[-1]
            if referenced_name in forbidden_names:
                offenders.append(offender(path, node.lineno, referenced_name))
            if isinstance(node, ast.ImportFrom) and node.module == "os.path":
                for alias in node.names:
                    if alias.name == "basename":
                        offenders.append(
                            offender(path, node.lineno, "os.path.basename")
                        )
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "basename"
                and expression_chain(node.value) in {("os", "path"), ("posixpath",)}
            ):
                offenders.append(offender(path, node.lineno, "basename"))
    assert offenders == []


def test_runtime_does_not_own_provider_retry_literals() -> None:
    retry_literal = "Selected model is at capacity. Please try a different model."
    offenders = [
        offender(path, node.lineno, retry_literal)
        for path in python_files(SRC_ROOT / "crewplane" / "runtime" / "agent")
        for node in walk_ast(parse_python(path))
        if isinstance(node, ast.Constant) and node.value == retry_literal
    ]
    assert offenders == []


def test_runtime_and_tmux_do_not_infer_presentation_from_provider_names() -> None:
    provider_literals = {"claude", "codex", "copilot", "gemini", "kilo"}
    checked_roots = (
        SRC_ROOT / "crewplane" / "runtime" / "execution",
        SRC_ROOT / "crewplane" / "observability" / "tmux",
    )
    offenders: list[str] = []
    for root in checked_roots:
        for path in python_files(root):
            for node in walk_ast(parse_python(path)):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in provider_literals
                ):
                    offenders.append(offender(path, node.lineno, node.value))
    assert offenders == []


def test_provider_usage_fallback_does_not_materialize_output_artifacts() -> None:
    checked_paths = (
        SRC_ROOT
        / "crewplane"
        / "runtime"
        / "execution"
        / "provider_call"
        / "__init__.py",
        SRC_ROOT
        / "crewplane"
        / "runtime"
        / "execution"
        / "provider_call"
        / "events.py",
    )
    fallback_calls = 0
    offenders: list[str] = []
    for path in checked_paths:
        for node in walk_ast(parse_python(path)):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node.func)
            if name == "read_text":
                offenders.append(offender(path, node.lineno, "read_text"))
            if name == "build_fallback_usage_from_output_file":
                fallback_calls += 1
    assert offenders == []
    assert fallback_calls > 0
