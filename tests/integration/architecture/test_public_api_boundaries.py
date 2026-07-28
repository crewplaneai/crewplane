from __future__ import annotations

import ast
from dataclasses import fields

from crewplane.artifacts.results.review_loop_status import ReviewLoopStatusError
from crewplane.runtime.execution.provider_call import ProviderCallRequest
from tests.integration.architecture.static_checks import (
    REPO_ROOT,
    SRC_ROOT,
    TESTS_ROOT,
    call_name,
    offender,
    parse_python,
    python_files,
    walk_ast,
)

LEGACY_EVENT_FIELDS = {
    "attempt_count",
    "attributes",
    "audit_round_num",
    "cli_captured",
    "configured_cost_usd",
    "duration_ms",
    "error",
    "failure_advice",
    "failure_kind",
    "failure_phase",
    "failure_source",
    "invocation_cost_confidence",
    "level",
    "log_file",
    "log_presentation_format",
    "log_presentation_profile",
    "message",
    "model",
    "node_id",
    "operation",
    "output_extraction_status",
    "output_file",
    "provider",
    "provider_tokens",
    "provider_usage_status",
    "role",
    "round_num",
    "task_id",
    "usage_parse_error",
    "visible_estimate_is_lower_bound",
    "visible_estimate_method",
    "visible_estimate_tokens",
}


def test_pep561_marker_is_packaged() -> None:
    marker = SRC_ROOT / "crewplane" / "py.typed"
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert marker.is_file()
    assert '"src/crewplane/py.typed"' in pyproject


def test_execution_events_do_not_use_legacy_flat_fields() -> None:
    offenders: list[str] = []
    for root in (SRC_ROOT, TESTS_ROOT):
        for path in python_files(root):
            for node in walk_ast(parse_python(path)):
                if (
                    not isinstance(node, ast.Call)
                    or call_name(node.func) != "ExecutionEvent"
                ):
                    continue
                legacy_keywords = sorted(
                    keyword.arg
                    for keyword in node.keywords
                    if keyword.arg in LEGACY_EVENT_FIELDS
                )
                if legacy_keywords:
                    offenders.append(offender(path, node.lineno, str(legacy_keywords)))
    assert offenders == []


def test_execution_event_has_no_legacy_flat_accessors() -> None:
    path = SRC_ROOT / "crewplane" / "observability" / "events" / "execution_event.py"
    offenders: list[str] = []
    for node in walk_ast(parse_python(path)):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name in LEGACY_EVENT_FIELDS
            and any(
                call_name(decorator) == "property" for decorator in node.decorator_list
            )
        ):
            offenders.append(offender(path, node.lineno, node.name))
    assert offenders == []


def test_public_package_exports_are_narrow() -> None:
    import crewplane.core as core_package
    import crewplane.runtime as runtime_package

    assert core_package.__all__ == ["SCHEMA_VERSION"]
    assert runtime_package.__all__ == []
    assert "__getattr__" not in vars(runtime_package)


def test_output_manager_directory_fields_are_read_only_properties() -> None:
    path = SRC_ROOT / "crewplane" / "artifacts" / "manager.py"
    managed_fields = {
        "base_dir",
        "log_cli_output",
        "logs_dir",
        "results_dir",
        "run_id",
        "stages_dir",
        "task_name",
    }
    assigned_fields: list[str] = []
    property_fields: set[str] = set()
    for node in parse_python(path).body:
        if not isinstance(node, ast.ClassDef) or node.name != "OutputManager":
            continue
        for member in node.body:
            if (
                isinstance(member, ast.FunctionDef)
                and member.name in managed_fields
                and any(
                    call_name(decorator) == "property"
                    for decorator in member.decorator_list
                )
            ):
                property_fields.add(member.name)
            if not isinstance(member, ast.FunctionDef) or member.name != "__init__":
                continue
            assigned_fields.extend(
                target.attr
                for child in walk_ast(member)
                for target in assignment_targets(child)
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr in managed_fields
                )
            )
    assert assigned_fields == []
    assert property_fields == managed_fields


def test_boundary_option_contracts_use_json_object() -> None:
    checked_paths = (
        SRC_ROOT / "crewplane" / "architecture",
        SRC_ROOT / "crewplane" / "bootstrap",
        SRC_ROOT / "crewplane" / "adapters",
    )
    forbidden_types = {"dict[str, Any]", "dict[str, object]"}
    offenders: list[str] = []
    for root in checked_paths:
        for path in python_files(root):
            for node in walk_ast(parse_python(path)):
                if isinstance(node, ast.Subscript):
                    rendered = ast.unparse(node)
                    if rendered in forbidden_types:
                        offenders.append(offender(path, node.lineno, rendered))
    assert offenders == []


def test_preflight_any_maps_are_limited_to_redaction_traversal() -> None:
    allowed_path = (
        SRC_ROOT
        / "crewplane"
        / "core"
        / "preflight"
        / "runtime_config"
        / "redaction.py"
    )
    offenders: list[str] = []
    for path in python_files(SRC_ROOT / "crewplane" / "core" / "preflight"):
        module = parse_python(path)
        for node in walk_ast(module):
            if not isinstance(node, ast.Subscript):
                continue
            if ast.unparse(node) != "dict[str, Any]":
                continue
            if path != allowed_path:
                offenders.append(offender(path, node.lineno, "dict[str, Any]"))
        if path == allowed_path:
            docstring = ast.get_docstring(module) or ""
            if "arbitrary JSON-compatible config snapshots" not in docstring:
                offenders.append(offender(path, 1, "missing rationale"))
    assert offenders == []


def test_review_loop_status_error_is_public() -> None:
    assert ReviewLoopStatusError.__name__ == "ReviewLoopStatusError"


def test_provider_call_request_does_not_carry_display_state() -> None:
    request_fields = {field.name for field in fields(ProviderCallRequest)}
    assert "progress_description" not in request_fields
    assert "show_console_summary" not in request_fields


def assignment_targets(node: ast.AST) -> tuple[ast.expr, ...]:
    if isinstance(node, ast.Assign):
        return tuple(node.targets)
    if isinstance(node, ast.AnnAssign):
        return (node.target,)
    return ()
