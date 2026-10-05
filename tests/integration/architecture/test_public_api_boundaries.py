from __future__ import annotations

import ast
import tomllib
from dataclasses import fields
from importlib.resources import files
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
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


def test_public_package_exports_are_narrow() -> None:
    import crewplane.core as core_package
    import crewplane.runtime as runtime_package

    assert core_package.__all__ == ["SCHEMA_VERSION"]
    assert runtime_package.__all__ == []
    assert "__getattr__" not in vars(runtime_package)


def test_provider_call_request_does_not_carry_display_state() -> None:
    request_fields = {field.name for field in fields(ProviderCallRequest)}
    assert "progress_description" not in request_fields
    assert "show_console_summary" not in request_fields


@pytest.mark.parametrize(
    "name",
    [
        "base_dir",
        "log_cli_output",
        "logs_dir",
        "results_dir",
        "run_id",
        "stages_dir",
        "task_name",
    ],
)
def test_output_manager_fields_are_read_only(tmp_path: Path, name: str) -> None:
    output = OutputManager("read-only", base_dir=tmp_path)
    original = getattr(output, name)
    with pytest.raises(AttributeError):
        setattr(output, name, "replacement")
    assert getattr(output, name) == original


def test_package_exposes_typing_marker() -> None:
    assert files("crewplane").joinpath("py.typed").is_file()


def test_execution_events_do_not_use_legacy_flat_fields() -> None:
    offenders: list[str] = []
    for root in (SRC_ROOT, TESTS_ROOT):
        for path in python_files(root):
            for node in ast.walk(parse_python(path)):
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
    for node in ast.walk(parse_python(path)):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name in LEGACY_EVENT_FIELDS
            and any(
                call_name(decorator) == "property" for decorator in node.decorator_list
            )
        ):
            offenders.append(offender(path, node.lineno, node.name))
    assert offenders == []


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
            for node in ast.walk(parse_python(path)):
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
        for node in ast.walk(module):
            if not isinstance(node, ast.Subscript):
                continue
            if ast.unparse(node) != "dict[str, Any]":
                continue
            if path != allowed_path:
                offenders.append(offender(path, node.lineno, "dict[str, Any]"))
    assert offenders == []


def test_review_loop_status_error_is_public() -> None:
    assert ReviewLoopStatusError.__name__ == "ReviewLoopStatusError"


def test_typing_marker_is_included_in_wheel_configuration() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "src/crewplane/py.typed" in wheel["include"]
