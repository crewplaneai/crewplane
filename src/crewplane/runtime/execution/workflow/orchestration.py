from __future__ import annotations

from crewplane.architecture.contracts import AgentInvoker
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.preflight.secrets import SecretContext
from crewplane.observability.events import EventSink

from ..resume import emit_resumed_node_events
from .execution_session import initialize_workflow_execution
from .postconditions import (
    collect_workflow_postconditions,
    raise_if_postcondition_errors,
)
from .scheduling import finalize_execution, run_scheduling_loop


async def execute_workflow(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    invoker: AgentInvoker,
    secret_context: SecretContext,
    event_sink: EventSink | None = None,
    run_id: str | None = None,
    suppress_progress_output: bool = False,
    workflow_identity: str | None = None,
    resumed_node_ids: tuple[str, ...] = (),
) -> None:
    """Execute a compiled preflight plan with optional live observability hooks."""
    session = initialize_workflow_execution(
        plan=plan,
        output=output,
        secret_context=secret_context,
        event_sink=event_sink,
        run_id=run_id,
        suppress_progress_output=suppress_progress_output,
        workflow_identity=workflow_identity,
        resumed_node_ids=resumed_node_ids,
    )
    scheduler_succeeded = False
    postcondition_errors: list[Exception] = []
    try:
        for node_id in sorted(
            resumed_node_ids,
            key=session.state.node_order.__getitem__,
        ):
            emit_resumed_node_events(node_id, session.telemetry)
        await run_scheduling_loop(session, output, invoker)
        await finalize_execution(session)
        scheduler_succeeded = True
    finally:
        postcondition_errors = await collect_workflow_postconditions(session, output)
    raise_if_postcondition_errors(
        scheduler_succeeded=scheduler_succeeded,
        postcondition_errors=postcondition_errors,
    )
