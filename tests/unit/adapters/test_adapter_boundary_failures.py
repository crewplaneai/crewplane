from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.adapters.invokers.cli_invoker.streaming import iter_stdout_json_objects
from crewplane.adapters.invokers.cli_invoker.usage_decoders import (
    decode_claude_usage,
    decode_codex_usage,
    decode_kilo_usage,
)
from crewplane.adapters.invokers.mock import MockInvokerAdapter
from crewplane.adapters.ui.null import NullUIAdapter
from crewplane.architecture.contracts import CommandResult, WorkflowTopology
from crewplane.architecture.errors import AdapterLoadError
from crewplane.architecture.loader import instantiate_adapter, load_adapter_class
from crewplane.core.config import AgentConfig, Config
from crewplane.version import SCHEMA_VERSION


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("config", {}, TypeError),
        ("workflow_topology", {}, TypeError),
        ("run_id", "", ValueError),
        ("run_id", 1, ValueError),
        ("console", object(), TypeError),
        ("warning_sink", "invalid", TypeError),
        ("which_fn", "invalid", TypeError),
    ],
)
def test_null_ui_rejects_invalid_runtime_boundary_values(
    field: str, value: object, error: type[Exception]
) -> None:
    request = {
        "config": Config(version=SCHEMA_VERSION, agents={}),
        "workflow_topology": WorkflowTopology("flow", ()),
        "run_id": "run",
        "console": Console(),
    }
    request[field] = value

    with pytest.raises(error, match=field):
        NullUIAdapter().create_runtime(**request)


@pytest.mark.parametrize("adapter", [CliInvokerAdapter(), MockInvokerAdapter()])
def test_invoker_factory_rejects_a_raw_configuration_mapping(adapter: object) -> None:
    with pytest.raises(TypeError, match="Config"):
        adapter.create_invoker({})


@pytest.mark.parametrize("kind", ["directory", "non-executable"])
def test_cli_plan_rejects_unlaunchable_absolute_executable(
    tmp_path: Path, kind: str
) -> None:
    executable = tmp_path / "provider"
    if kind == "directory":
        executable.mkdir()
    else:
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o600)
    agent = AgentConfig(cli_cmd=[str(executable)], provider_kind="generic")

    with pytest.raises(FileNotFoundError if kind == "directory" else PermissionError):
        build_cli_invocation_plan(agent, "model", "prompt", tmp_path / "result.md")

    assert not (tmp_path / "result.md").exists()


def test_adapter_loader_reports_missing_class_separately_from_import_failure() -> None:
    with pytest.raises(AdapterLoadError, match="does not define 'MissingAdapter'"):
        load_adapter_class("ui", "crewplane.adapters.ui.null:MissingAdapter")


def test_adapter_loader_retains_constructor_failure() -> None:
    with pytest.raises(AdapterLoadError, match="constructor failed") as caught:
        instantiate_adapter("ui", f"{__name__}:BrokenUIAdapter")

    assert isinstance(caught.value.__cause__, RuntimeError)


class BrokenUIAdapter(NullUIAdapter):
    def __init__(self) -> None:
        raise RuntimeError("constructor failed")


def test_json_event_iterator_stops_after_malformed_input() -> None:
    result = CommandResult(0, '\n{"first":true}\ninvalid\n{"later":true}', "")

    assert list(iter_stdout_json_objects(result)) == [{"first": True}, None]


@pytest.mark.parametrize(
    ("provider", "stdout", "error"),
    [
        ("claude", "{", "Malformed"),
        ("claude", '{"modelUsage":[]}', "Malformed Claude modelUsage"),
        (
            "kilo",
            '\n{"type":"step_finish","part":{"tokens":[]}}',
            "Malformed Kilo token",
        ),
    ],
)
def test_usage_decoders_reject_malformed_reports_without_fabricating_tokens(
    provider: str, stdout: str, error: str
) -> None:
    decoder = decode_claude_usage if provider == "claude" else decode_kilo_usage
    decoded = decoder(CommandResult(0, stdout, ""))

    assert decoded.error is not None
    assert error in decoded.error
    assert decoded.tokens is None
    assert decoded.valid_report_count == 0


def test_codex_usage_ignores_blank_stream_lines_before_valid_report() -> None:
    decoded = decode_codex_usage(
        CommandResult(0, '\n{"type":"turn.completed","usage":{"input_tokens":1}}', "")
    )

    assert decoded.tokens is not None
    assert decoded.tokens.input == 1
    assert decoded.valid_report_count == 1
    assert decoded.error is None
