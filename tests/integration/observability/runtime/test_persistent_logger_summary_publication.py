import tempfile
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from crewplane.architecture.contracts import EventType
from crewplane.artifacts import OutputManager
from crewplane.artifacts.atomic import atomic_write_text
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability import PersistentRunLogger
from crewplane.observability.events import (
    format_execution_event_log_line,
)
from crewplane.observability.persistent import (
    render_run_summary_markdown,
    render_run_summary_terminal,
)
from crewplane.observability.run_summary.models import (
    RunSummary,
)
from crewplane.observability.types import (
    RunContext,
    RunResult,
)
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    single_node_workflow,
)


def test_refresh_summary_reloads_exact_totals_from_disk() -> None:
    workflow = single_node_workflow()
    with tempfile.TemporaryDirectory() as tmp_dir:
        output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
        persistent_logger = PersistentRunLogger(output)
        context = RunContext(
            workflow_topology=topology_from_workflow(workflow),
            run_id=output.run_id,
            refresh_per_second=0,
        )
        persistent_logger.start(context)
        disk_only_event = make_execution_event(
            event_type=EventType.INVOCATION_FAILED,
            workflow_name=workflow.name,
            run_id=output.run_id,
            node_id="node.a",
            provider="gemini",
            role=ProviderRole.EXECUTOR,
            task_id="disk-only",
            attempt_count=2,
            provider_usage_status="full",
            provider_usage_report_count=2,
            provider_tokens={"input": 7, "output": 3, "total": 10},
        )
        with output.get_run_event_log_path().open("a", encoding="utf-8") as handle:
            handle.write(format_execution_event_log_line(disk_only_event))

        summary = persistent_logger.refresh_summary(RunResult(status="failed"))

        assert summary is not None
        assert summary is not None
        aggregate = summary.provider_token_aggregates.overall
        assert aggregate is not None
        assert aggregate is not None
        assert aggregate.report_count == 2
        assert aggregate.input == 7
        assert aggregate.output == 3
        assert summary.workflow_status == "failed"
        terminal = render_run_summary_terminal(summary)
        assert "Provider-reported tokens: 10 across 2 reports" in terminal


def test_stop_writes_bounded_summary_without_reading_event_log() -> None:
    workflow = single_node_workflow()
    with tempfile.TemporaryDirectory() as tmp_dir:
        output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
        persistent_logger = PersistentRunLogger(output)
        persistent_logger.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                refresh_per_second=0,
            )
        )
        persistent_logger.record_event(
            make_execution_event(
                event_type=EventType.INVOCATION_FINISHED,
                workflow_name=workflow.name,
                run_id=output.run_id,
                node_id="node.a",
                provider="codex",
                role=ProviderRole.EXECUTOR,
                task_id="bounded-stop",
                attempt_count=1,
                provider_usage_report_count=1,
                provider_tokens={"input": 7, "output": 3, "total": 10},
            )
        )

        with patch(
            "crewplane.observability.run_summary.logger.read_event_log",
            side_effect=AssertionError("stop must not read the event log"),
        ):
            persistent_logger.stop(RunResult(status="succeeded"))

        bounded_summary = persistent_logger.last_summary
        assert bounded_summary is not None
        assert bounded_summary is not None
        assert bounded_summary.workflow_status == "succeeded"
        assert bounded_summary.provider_token_aggregates.overall is None

        durable_summary = persistent_logger.refresh_summary(
            RunResult(status="succeeded")
        )

        assert durable_summary is not None
        assert durable_summary is not None
        aggregate = durable_summary.provider_token_aggregates.overall
        assert aggregate is not None
        assert aggregate is not None
        assert aggregate.report_count == 1
        assert aggregate.total == 10


def test_summary_publication_uses_atomic_write() -> None:
    workflow = single_node_workflow()
    with tempfile.TemporaryDirectory() as tmp_dir:
        output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
        persistent_logger = PersistentRunLogger(output)
        persistent_logger.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                refresh_per_second=0,
            )
        )
        published_paths: list[Path] = []

        def record_atomic_write(
            path: Path,
            content: str,
            ensure_parent: bool = True,
        ) -> Path:
            published_paths.append(path)
            return atomic_write_text(path, content, ensure_parent)

        with patch(
            "crewplane.observability.run_summary.logger.atomic_write_text",
            new=record_atomic_write,
        ):
            persistent_logger.stop(RunResult(status="succeeded"))

        assert published_paths == [output.get_run_summary_path()]


def test_exact_summary_wins_over_late_bounded_stop_write() -> None:
    workflow = single_node_workflow()
    with tempfile.TemporaryDirectory() as tmp_dir:
        output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
        persistent_logger = PersistentRunLogger(output)
        persistent_logger.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                refresh_per_second=0,
            )
        )
        persistent_logger.record_event(
            make_execution_event(
                event_type=EventType.INVOCATION_FINISHED,
                workflow_name=workflow.name,
                run_id=output.run_id,
                node_id="node.a",
                provider="codex",
                role=ProviderRole.EXECUTOR,
                task_id="late-stop",
                attempt_count=1,
                provider_usage_report_count=1,
                provider_tokens={"input": 7, "output": 3, "total": 10},
            )
        )
        bounded_render_started = Event()
        release_bounded_render = Event()

        def delay_bounded_summary_render(summary: RunSummary) -> str:
            if summary.provider_token_aggregates.overall is None:
                bounded_render_started.set()
                if not release_bounded_render.wait(timeout=5):
                    raise TimeoutError("Bounded summary render was not released.")
            return render_run_summary_markdown(summary)

        stop_thread = Thread(
            target=persistent_logger.stop,
            args=(RunResult(status="succeeded"),),
            name="crewplane-test-late-summary-stop",
            daemon=True,
        )
        with patch(
            "crewplane.observability.run_summary.logger.render_run_summary_markdown",
            new=delay_bounded_summary_render,
        ):
            stop_thread.start()
            assert bounded_render_started.wait(timeout=5)
            try:
                exact_summary = persistent_logger.refresh_summary(
                    RunResult(status="succeeded")
                )
            finally:
                release_bounded_render.set()
            stop_thread.join(timeout=5)

        assert not stop_thread.is_alive()
        assert exact_summary is not None
        assert exact_summary is not None
        exact_aggregate = exact_summary.provider_token_aggregates.overall
        assert exact_aggregate is not None
        assert exact_aggregate is not None
        assert exact_aggregate.total == 10

        final_summary = persistent_logger.last_summary
        assert final_summary is not None
        assert final_summary is not None
        final_aggregate = final_summary.provider_token_aggregates.overall
        assert final_aggregate is not None
        assert final_aggregate is not None
        assert final_aggregate.total == 10
        summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
        assert "Provider-reported tokens: 10 across 1 reports" in summary_text
