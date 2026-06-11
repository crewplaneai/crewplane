from __future__ import annotations

from pathlib import Path

from orchestrator_cli.architecture.contracts import InvocationContext
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.runtime.execution.execution_activity import ExecutionTelemetry
from orchestrator_cli.runtime.execution.execution_events import RuntimeEventContext
from orchestrator_cli.runtime.execution.log_presentation import (
    resolve_log_presentation_descriptor,
)


class InvalidDescriptorInvoker:
    async def invoke(
        self,
        _config: AgentConfig,
        _model: str | None,
        _prompt: str,
        _output_file: Path,
        _log_file: Path | None = None,
        _invocation_context: InvocationContext | None = None,
    ) -> None:
        del (
            _config,
            _model,
            _prompt,
            _output_file,
            _log_file,
            _invocation_context,
        )
        raise AssertionError("not used")

    def log_presentation_for(self, _config: AgentConfig) -> object:
        del _config
        return {"format": "json_lines", "profile": "unsafe/profile"}


def test_invalid_log_presentation_descriptor_warns_and_falls_back() -> None:
    events: list[ExecutionEvent] = []
    telemetry = ExecutionTelemetry(
        workflow_name="workflow",
        run_id="run-1",
        event_sink=events.append,
        suppress_console_output=True,
    )

    descriptor = resolve_log_presentation_descriptor(
        InvalidDescriptorInvoker(),
        AgentConfig(cli_cmd=["mock"]),
        telemetry,
        RuntimeEventContext(node_id="node.a", provider="alpha"),
    )

    assert descriptor is None
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "runtime_log"
    assert event.payload.operation == "log_presentation_descriptor_invalid"
    assert "unsafe/profile" not in event.payload.message
    assert event.payload.attributes == {"reason": "ValueError"}
