import asyncio
import io

from rich.console import Console

from crewplane.architecture.contracts import CommandResult
from crewplane.bootstrap import build_runtime_components
from crewplane.core.config import AgentConfig, Config
from crewplane.core.workflow.models import ProviderSpec, WorkflowNode, WorkflowPlan
from crewplane.observability.log_presentation import format_log_file
from crewplane.version import SCHEMA_VERSION
from tests.helpers.observability import topology_from_workflow
from tests.helpers.opencode import fixture_text


def test_existing_cli_composition_uses_opencode_and_generic_formatter(
    tmp_path, monkeypatch
):
    agent = AgentConfig(cli_cmd=["opencode", "run"], provider_kind="opencode")
    workflow = WorkflowPlan(
        name="OpenCode wiring",
        nodes=[
            WorkflowNode(
                id="node",
                mode="parallel",
                providers=[ProviderSpec(provider="local-worker")],
            )
        ],
    )
    components = build_runtime_components(
        config=Config(version=SCHEMA_VERSION, agents={"local-worker": agent}),
        workflow_topology=topology_from_workflow(workflow),
        state_dir=tmp_path / ".crewplane",
        project_root=tmp_path,
        console=Console(file=io.StringIO()),
        no_live=True,
    )
    descriptor = components.base_invoker.log_presentation_for(agent)
    assert descriptor is not None
    assert (descriptor.format, descriptor.profile) == ("json_lines", "generic")
    calls = []

    async def runner(**kwargs):
        calls.append(kwargs["cmd"])
        return CommandResult(0, fixture_text(), "")

    monkeypatch.setattr(
        "crewplane.runtime.agent.invocation.command.run_command_once", runner
    )
    output = tmp_path / "answer.md"
    asyncio.run(components.base_invoker.invoke(agent, None, "prompt", output, tmp_path))
    assert calls[0][1:] == ["run", "--format", "json", "--dir", str(tmp_path)]
    assert output.read_text() == "Answer λ\n"
    log = tmp_path / "events.log"
    log.write_text(fixture_text())
    snapshot = format_log_file(log, descriptor, line_budget=50)
    assert any("Answer" in line for line in snapshot.lines)
    assert log.read_text() == fixture_text()
