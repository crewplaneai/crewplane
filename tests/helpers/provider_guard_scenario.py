import asyncio
import sys
import time
from pathlib import Path

from crewplane.artifacts.locks import acquire_same_context_lock
from crewplane.artifacts.manager import OutputManager
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.execution.activity.events import InvocationMetadata
from crewplane.runtime.execution.provider_call.display import ProviderCallDisplay
from crewplane.runtime.execution.provider_call.events import build_invocation_context
from crewplane.runtime.execution.publication_registry import RuntimePublicationRegistry


def main() -> None:
    mode = sys.argv[1]
    state_dir = Path(sys.argv[2])
    workflow_name = sys.argv[3]
    workflow_identity = sys.argv[4]
    workflow_signature = sys.argv[5]
    lock = acquire_same_context_lock(
        state_dir,
        workflow_name,
        workflow_identity,
        workflow_signature,
        grace_seconds=0,
    )
    output = OutputManager(workflow_name, base_dir=state_dir)
    lock.update_run(output.run_id, output.run_key_name)
    context, _ = build_invocation_context(
        telemetry=None,
        metadata=InvocationMetadata(
            node_id="build.node",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            model=None,
            task_id="generic_executor_0",
            audit_round_num=None,
            round_num=1,
            output_file=output.stages_dir / "provider-output.md",
            log_file=None,
        ),
        display=ProviderCallDisplay(telemetry=None),
        output=output,
        runtime_publications=RuntimePublicationRegistry(),
    )
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    if mode == "descendant":
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/processes/provider_descendant.py"
        )
        command = [
            sys.executable,
            str(fixture),
            str(state_dir / "provider-descendant.pid"),
        ]
    asyncio.run(
        run_command_once(
            cmd=command,
            stdin_data=None,
            log_file=None,
            append_log=False,
            log_header=None,
            cwd=state_dir,
            invocation_context=context,
            idle_timeout_seconds=None,
        )
    )

    if mode == "descendant":
        time.sleep(60)


if __name__ == "__main__":
    main()
