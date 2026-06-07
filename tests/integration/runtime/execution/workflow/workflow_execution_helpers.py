import asyncio
from datetime import datetime
from pathlib import Path
from threading import Event

from rich.console import Console

from orchestrator_cli.artifacts import OutputManager, safe_artifact_name
from orchestrator_cli.bootstrap import build_runtime_config_snapshot
from orchestrator_cli.core.config import AgentConfig, Config
from orchestrator_cli.core.preflight import (
    PreflightCompileOptions,
    PreflightExecutionPlan,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from orchestrator_cli.core.workflow_models import (
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.runtime.agent.types import AgentInvoker
from orchestrator_cli.runtime.execution import (
    execute_parallel_stage as _execute_compiled_parallel_stage,
)
from orchestrator_cli.runtime.execution import (
    execute_sequential_stage as _execute_compiled_sequential_stage,
)
from orchestrator_cli.runtime.execution import (
    execute_workflow as _execute_compiled_workflow,
)
from orchestrator_cli.runtime.execution.common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
)
from orchestrator_cli.runtime.execution.consensus import (
    ParsedReviewResult,
    render_review_contract,
)


class MockAgentInvoker(AgentInvoker):
    def __init__(self, outputs: list[str] | None = None) -> None:
        self.outputs = outputs or []
        self.calls: list[dict[str, str | int | None]] = []

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double or callback signature.
        model: str,
        prompt: str,
        output_file: Path,
        log_file: Path | None = None,  # noqa: ARG002 - Required by test double or callback signature.
        invocation_context=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "output_file": str(output_file),
                "task_id": (
                    invocation_context.task_id
                    if invocation_context is not None
                    else None
                ),
                "role": (
                    invocation_context.role if invocation_context is not None else None
                ),
                "audit_round_num": (
                    invocation_context.audit_round_num
                    if invocation_context is not None
                    else None
                ),
                "round_num": (
                    invocation_context.round_num
                    if invocation_context is not None
                    else None
                ),
            }
        )
        call_index = len(self.calls) - 1
        content = self.outputs[call_index] if call_index < len(self.outputs) else "ok"
        output_file.write_text(content, encoding="utf-8")


class GraphDependencyOrderInvoker(AgentInvoker):
    def __init__(self) -> None:
        self.first_completed = False
        self.calls: list[str] = []

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double callback signature.
        model: str,  # noqa: ARG002 - Required by test double callback signature.
        prompt: str,  # noqa: ARG002 - Required by test double callback signature.
        output_file: Path,
        log_file: Path | None = None,  # noqa: ARG002 - Required by test double callback signature.
        invocation_context=None,  # type: ignore[no-untyped-def]
    ) -> None:
        assert invocation_context is not None
        self.calls.append(invocation_context.node_id)
        if invocation_context.node_id == "first":
            await asyncio.sleep(0.05)
            self.first_completed = True
            output_file.write_text("first done", encoding="utf-8")
            return
        if not self.first_completed:
            raise AssertionError("second started before dependency graph was satisfied")
        output_file.write_text("second done", encoding="utf-8")


def _compile_test_plan(
    config: Config,
    workflow: WorkflowPlan,
    output: OutputManager,
) -> tuple[PreflightExecutionPlan, CompiledRuntimeContext]:
    snapshot = build_runtime_config_snapshot(
        config=config,
        workflow_schema_version=workflow.schema_version,
        console=Console(file=None),
        no_live=True,
    )
    preview = compile_preflight_preview(
        source=PreflightWorkflowSource.from_workflow(
            workflow,
            workflow_content="test workflow",
            composed_workflow={
                "schema_version": workflow.schema_version,
                "name": workflow.name,
                "description": workflow.description,
                "inputs": dict(workflow.inputs),
                "nodes": [],
            },
        ),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=output.base_dir,
            orchestrator_dir=output.base_dir,
            fingerprint_key_policy="read_only",
        ),
    )
    if preview.diagnostics:
        messages = "; ".join(
            f"{diagnostic.code}: {diagnostic.message}"
            for diagnostic in preview.diagnostics
        )
        raise AssertionError(f"Unexpected test preflight diagnostics: {messages}")
    plan = PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id=output.run_id,
        run_key_name=output.stages_dir.name,
        context_root=output.stages_dir.as_posix(),
        manifest_root=(output.stages_dir / "manifests").as_posix(),
        created_at=datetime(2026, 6, 3),
    )
    for content_ref, payload in preview.static_file_payloads.items():
        output.write_preflight_static_file(content_ref, payload)
    return plan, CompiledRuntimeContext(
        plan=plan,
        secret_context=preview.secret_context,
    )


async def execute_workflow(
    config: Config,
    workflow: WorkflowPlan,
    output: OutputManager,
    invoker: AgentInvoker,
    event_sink=None,  # type: ignore[no-untyped-def]
    run_id: str | None = None,
    suppress_progress_output: bool = False,
) -> None:
    plan, runtime_context = _compile_test_plan(config, workflow, output)
    await _execute_compiled_workflow(
        plan=plan,
        output=output,
        invoker=invoker,
        secret_context=runtime_context.secret_context,
        event_sink=event_sink,
        run_id=run_id,
        suppress_progress_output=suppress_progress_output,
    )


async def execute_sequential_stage(
    config: Config,
    node: WorkflowNode,
    output: OutputManager,
    invoker: AgentInvoker,
    telemetry: ExecutionTelemetry | None = None,
) -> None:
    workflow = WorkflowPlan(name=output.task_name, nodes=[node])
    plan, runtime_context = _compile_test_plan(config, workflow, output)
    await _execute_compiled_sequential_stage(
        stage=plan.nodes[0],
        output=output,
        runtime_context=runtime_context,
        invoker=invoker,
        telemetry=telemetry,
    )


async def execute_parallel_stage(
    config: Config,
    node: WorkflowNode,
    output: OutputManager,
    invoker: AgentInvoker,
    telemetry: ExecutionTelemetry | None = None,
) -> None:
    workflow = WorkflowPlan(name=output.task_name, nodes=[node])
    plan, runtime_context = _compile_test_plan(config, workflow, output)
    await _execute_compiled_parallel_stage(
        stage=plan.nodes[0],
        output=output,
        runtime_context=runtime_context,
        invoker=invoker,
        telemetry=telemetry,
    )


class OptionalOutputInvoker(AgentInvoker):
    def __init__(self, outputs: list[str | None]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, str | int | None]] = []

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double or callback signature.
        model: str,  # noqa: ARG002 - Required by test double or callback signature.
        prompt: str,  # noqa: ARG002 - Required by test double or callback signature.
        output_file: Path,
        log_file: Path | None = None,  # noqa: ARG002 - Required by test double or callback signature.
        invocation_context=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.calls.append(
            {
                "role": (
                    invocation_context.role if invocation_context is not None else None
                ),
                "round_num": (
                    invocation_context.round_num
                    if invocation_context is not None
                    else None
                ),
            }
        )
        call_index = len(self.calls) - 1
        content = self.outputs[call_index] if call_index < len(self.outputs) else "ok"
        if content is None:
            return
        output_file.write_text(content, encoding="utf-8")


class ArtifactDriftInvoker(AgentInvoker):
    def __init__(
        self,
        outputs: list[str],
        mutations_by_call: dict[int, list[tuple[Path, str]]] | None = None,
        append_mutations_by_call: dict[int, list[tuple[Path, str]]] | None = None,
    ) -> None:
        self.outputs = outputs
        self.mutations_by_call = mutations_by_call or {}
        self.append_mutations_by_call = append_mutations_by_call or {}
        self.calls: list[dict[str, str | int | None]] = []

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double or callback signature.
        model: str,  # noqa: ARG002 - Required by test double or callback signature.
        prompt: str,  # noqa: ARG002 - Required by test double or callback signature.
        output_file: Path,
        log_file: Path | None = None,  # noqa: ARG002 - Required by test double or callback signature.
        invocation_context=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.calls.append(
            {
                "task_id": (
                    invocation_context.task_id
                    if invocation_context is not None
                    else None
                ),
                "role": (
                    invocation_context.role if invocation_context is not None else None
                ),
                "round_num": (
                    invocation_context.round_num
                    if invocation_context is not None
                    else None
                ),
            }
        )
        call_index = len(self.calls) - 1
        output_file.write_text(self.outputs[call_index], encoding="utf-8")
        for mutation_path, content in self.mutations_by_call.get(call_index, []):
            mutation_path.parent.mkdir(parents=True, exist_ok=True)
            mutation_path.write_text(content, encoding="utf-8")
        for mutation_path, content in self.append_mutations_by_call.get(call_index, []):
            mutation_path.parent.mkdir(parents=True, exist_ok=True)
            with mutation_path.open("a", encoding="utf-8") as handle:
                handle.write(content)


class SelectiveFailInvoker(AgentInvoker):
    def __init__(self, failing_models: set[str]) -> None:
        self.failing_models = failing_models
        self.calls: list[dict[str, str]] = []

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double or callback signature.
        model: str,
        prompt: str,
        output_file: Path,
        log_file: Path | None = None,  # noqa: ARG002 - Required by test double or callback signature.
        invocation_context=None,  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by test double or callback signature.
    ) -> None:
        self.calls.append(
            {"model": model, "prompt": prompt, "output_file": str(output_file)}
        )
        if model in self.failing_models:
            raise RuntimeError(f"simulated failure for {model}")
        output_file.write_text(f"success: {model}", encoding="utf-8")


class FindingsSelectiveFailInvoker(AgentInvoker):
    def __init__(self, failing_models: set[str]) -> None:
        self.failing_models = failing_models
        self.calls: list[dict[str, str]] = []

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double or callback signature.
        model: str,
        prompt: str,  # noqa: ARG002 - Required by test double or callback signature.
        output_file: Path,
        log_file: Path | None = None,  # noqa: ARG002 - Required by test double or callback signature.
        invocation_context=None,  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by test double or callback signature.
    ) -> None:
        self.calls.append({"model": model, "output_file": str(output_file)})
        if model in self.failing_models:
            raise RuntimeError(f"simulated failure for {model}")
        output_file.write_text(
            "\n".join(
                [
                    f"full output: {model}",
                    "",
                    "<!-- findings -->",
                    f"- concise finding: {model}",
                    "<!-- /findings -->",
                ]
            ),
            encoding="utf-8",
        )


class DelayByModelInvoker(AgentInvoker):
    def __init__(self, delays: dict[str, float]) -> None:
        self.delays = delays
        self.calls: list[str] = []

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double or callback signature.
        model: str,
        prompt: str,  # noqa: ARG002 - Required by test double or callback signature.
        output_file: Path,
        log_file: Path | None = None,  # noqa: ARG002 - Required by test double or callback signature.
        invocation_context=None,  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by test double or callback signature.
    ) -> None:
        self.calls.append(model)
        await asyncio.sleep(self.delays.get(model, 0))
        output_file.write_text(f"done: {model}", encoding="utf-8")


class FailingLogOutputManager(OutputManager):
    def __init__(
        self,
        task_name: str,
        base_dir: Path,
        failing_provider: str,
    ) -> None:
        super().__init__(task_name, base_dir=base_dir, log_cli_output=True)
        self.failing_provider = failing_provider

    def get_log_file(
        self,
        stage_name: str,
        provider: str,
        task_id: str,
        audit_round_num: int | None = None,
        round_num: int | None = None,
    ) -> Path | None:
        if provider == self.failing_provider:
            raise RuntimeError("log setup failed")
        return super().get_log_file(
            stage_name,
            provider,
            task_id,
            audit_round_num,
            round_num,
        )


class TimedTaskOutputInvoker(AgentInvoker):
    def __init__(
        self,
        outputs_by_task_id: dict[str, str],
        delays_by_task_id: dict[str, float] | None = None,
    ) -> None:
        self.outputs_by_task_id = outputs_by_task_id
        self.delays_by_task_id = delays_by_task_id or {}
        self.calls: list[dict[str, str | int | None]] = []

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double or callback signature.
        model: str,  # noqa: ARG002 - Required by test double or callback signature.
        prompt: str,  # noqa: ARG002 - Required by test double or callback signature.
        output_file: Path,
        log_file: Path | None = None,  # noqa: ARG002 - Required by test double or callback signature.
        invocation_context=None,  # type: ignore[no-untyped-def]
    ) -> None:
        assert invocation_context is not None
        task_id = invocation_context.task_id
        self.calls.append(
            {
                "node_id": invocation_context.node_id,
                "task_id": task_id,
                "role": invocation_context.role,
                "round_num": invocation_context.round_num,
            }
        )
        await asyncio.sleep(self.delays_by_task_id.get(task_id, 0))
        content = self.outputs_by_task_id.get(task_id)
        if content is None:
            content = (
                review_output(verdict="NO_FINDINGS")
                if invocation_context.role == "reviewer"
                else f"output for {task_id}"
            )
        output_file.write_text(content, encoding="utf-8")


class ParallelReviewerTimingInvoker(AgentInvoker):
    def __init__(self, reviewer_delay: float) -> None:
        self.reviewer_delay = reviewer_delay
        self.started_at: dict[str, float] = {}

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double or callback signature.
        model: str,  # noqa: ARG002 - Required by test double or callback signature.
        prompt: str,  # noqa: ARG002 - Required by test double or callback signature.
        output_file: Path,
        log_file: Path | None = None,  # noqa: ARG002 - Required by test double or callback signature.
        invocation_context=None,  # type: ignore[no-untyped-def]
    ) -> None:
        assert invocation_context is not None
        self.started_at[invocation_context.task_id] = asyncio.get_running_loop().time()
        if invocation_context.role == "reviewer":
            await asyncio.sleep(self.reviewer_delay)
            output_file.write_text(
                review_output(verdict="NO_FINDINGS"), encoding="utf-8"
            )
            return
        output_file.write_text("executor output", encoding="utf-8")


class CleanupOnCancelInvoker(AgentInvoker):
    def __init__(self, cleanup_delay_seconds: float = 0.05) -> None:
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_finished = asyncio.Event()
        self.cleanup_delay_seconds = cleanup_delay_seconds

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double or callback signature.
        model: str,  # noqa: ARG002 - Required by test double or callback signature.
        prompt: str,  # noqa: ARG002 - Required by test double or callback signature.
        output_file: Path,  # noqa: ARG002 - Required by test double or callback signature.
        log_file: Path | None = None,  # noqa: ARG002 - Required by test double or callback signature.
        invocation_context=None,  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by test double or callback signature.
    ) -> None:
        self.started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cleanup_started.set()
            await asyncio.sleep(self.cleanup_delay_seconds)
            self.cleanup_finished.set()
            raise


class BlockingSnapshotObserver:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()

    def start(self, context) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback signature.
        return

    def on_snapshot(self, event, snapshot) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback signature.
        self.entered.set()
        self.release.wait()

    def stop(self, result) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by callback signature.
        return


def review_output(
    major: str = "None",
    minor: str = "None",
    nitpicks: str = "None",
    verdict: str = "NO_FINDINGS",
) -> str:
    return render_review_contract(
        ParsedReviewResult(
            verdict=verdict,
            major_issues=major,
            minor_issues=minor,
            nitpicks=nitpicks,
        )
    )


def review_state_path(node_dir: Path, task_id: str, round_num: int) -> Path:
    return (
        node_dir
        / "review-state"
        / f"{safe_artifact_name(task_id)}-round-{round_num}.state.json"
    )


def review_inbox_path(node_dir: Path, round_num: int) -> Path:
    return node_dir / "review-state" / f"review-inbox-round-{round_num}.md"


def review_loop_status_path(node_dir: Path) -> Path:
    return node_dir / "review-state" / "review-loop-status.json"


def audit_round_dir(node_dir: Path, audit_round_num: int) -> Path:
    return node_dir / f"review-audit-round-{audit_round_num}"
