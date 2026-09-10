import tempfile
from pathlib import Path

from crewplane.adapters.invokers.cli_invoker.usage_decoders import decode_codex_usage
from crewplane.architecture.contracts import CommandResult, EventType, ProviderKind
from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability import PersistentRunLogger
from crewplane.observability.persistent import (
    render_run_summary_markdown,
    render_run_summary_terminal,
)
from crewplane.observability.run_summary.accumulator import (
    MAX_RETAINED_INVOCATION_USAGE_DETAILS,
)
from crewplane.observability.run_summary.logger import (
    MAX_RETAINED_SUMMARY_EVENTS,
)
from crewplane.observability.runtime import ObservabilityHub
from crewplane.observability.types import (
    RunContext,
    RunResult,
)
from crewplane.runtime.agent.usage import InvocationUsageAccumulator
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    single_node_workflow,
)


def test_persistent_run_logger_bounds_retained_event_details() -> None:
    workflow = single_node_workflow()
    overflow_count = 5
    with tempfile.TemporaryDirectory() as tmp_dir:
        output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
        persistent_logger = PersistentRunLogger(output)

        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id=output.run_id,
            observers=[persistent_logger],
            refresh_per_second=0,
        ) as hub:
            for index in range(MAX_RETAINED_SUMMARY_EVENTS + overflow_count):
                hub.emit(
                    make_execution_event(
                        event_type=EventType.RUNTIME_LOG,
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        level="warning",
                        message=f"summary warning {index}",
                        operation="summary_retention_test",
                    )
                )

        assert persistent_logger.retained_event_count == MAX_RETAINED_SUMMARY_EVENTS
        assert persistent_logger.dropped_event_count == overflow_count

        event_log_lines = (
            output.get_run_event_log_path().read_text(encoding="utf-8").splitlines()
        )
        assert len(event_log_lines) == MAX_RETAINED_SUMMARY_EVENTS + overflow_count
        summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
        assert "were omitted from in-memory summary detail" in summary_text
        assert (
            f"summary warning {MAX_RETAINED_SUMMARY_EVENTS + overflow_count - 1}"
            in summary_text
        )


def test_summary_rollups_survive_retained_event_detail_cap() -> None:
    workflow = single_node_workflow()
    overflow_count = 5
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
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha_executor_0",
                attempt_count=1,
                cli_captured=True,
                output_extraction_status="success",
                provider_usage_status="full",
                provider_tokens={
                    "input": 100,
                    "cached_input": None,
                    "cache_write": None,
                    "output": 20,
                    "reasoning": None,
                    "total": None,
                },
                visible_estimate_tokens=50,
                visible_estimate_method="char-count-lower-bound",
                visible_estimate_is_lower_bound=True,
                configured_cost_usd=0.0004,
                invocation_cost_confidence="full",
            )
        )
        for index in range(MAX_RETAINED_SUMMARY_EVENTS + overflow_count):
            persistent_logger.record_event(
                make_execution_event(
                    event_type=EventType.RUNTIME_LOG,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    level="warning",
                    message=f"summary warning {index}",
                    operation="summary_retention_test",
                )
            )

        persistent_logger.stop(RunResult(status="succeeded"))

        assert persistent_logger.dropped_event_count == overflow_count + 1
        summary = persistent_logger.last_summary
        assert summary is not None
        assert summary is not None
        assert summary.spend is not None
        assert summary.spend is not None
        assert summary.spend.terminal_invocations == 1
        assert summary.spend.provider_usage_full_invocations == 1
        assert summary.spend.configured_cost_usd == 0.0004
        assert len(summary.provider_rollups) == 1
        assert summary.provider_rollups[0].provider == "alpha"


def test_invocation_usage_details_are_bounded_without_losing_rollups() -> None:
    workflow = single_node_workflow()
    overflow_count = 7
    invocation_count = MAX_RETAINED_INVOCATION_USAGE_DETAILS + overflow_count
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
        for index in range(invocation_count):
            persistent_logger.record_event(
                make_execution_event(
                    event_type=EventType.INVOCATION_FINISHED,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    task_id=f"alpha_task_{index:04d}",
                    attempt_count=1,
                    cli_captured=True,
                    output_extraction_status="success",
                    provider_usage_status="full",
                    provider_tokens={
                        "input": 10,
                        "cached_input": None,
                        "cache_write": None,
                        "output": 2,
                        "reasoning": None,
                        "total": None,
                    },
                    visible_estimate_tokens=12,
                    visible_estimate_method="char-count-lower-bound",
                    visible_estimate_is_lower_bound=True,
                    configured_cost_usd=0.0001,
                    invocation_cost_confidence="full",
                )
            )

        persistent_logger.stop(RunResult(status="succeeded"))

        summary = persistent_logger.last_summary
        assert summary is not None
        assert summary is not None
        assert len(summary.invocation_usages) == MAX_RETAINED_INVOCATION_USAGE_DETAILS
        assert summary.omitted_invocation_usage_count == overflow_count
        assert summary.spend is not None
        assert summary.spend is not None
        assert summary.spend.terminal_invocations == invocation_count
        assert summary.spend.total_attempts == invocation_count
        assert summary.spend.provider_usage_full_invocations == invocation_count
        assert len(summary.provider_rollups) == 1
        assert summary.provider_rollups[0].terminal_invocations == invocation_count
        assert (
            summary.invocation_usages[0].task_id == f"alpha_task_{overflow_count:04d}"
        )

        summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
        assert f"- Terminal invocations: {invocation_count}" in summary_text
        assert (
            "Invocation detail: retained latest "
            f"{MAX_RETAINED_INVOCATION_USAGE_DETAILS} invocation(s); "
            f"{overflow_count} earlier invocation detail(s)" in summary_text
        )
        assert "alpha_task_0000" not in summary_text
        assert f"alpha_task_{invocation_count - 1:04d}" in summary_text
        event_log_lines = (
            output.get_run_event_log_path().read_text(encoding="utf-8").splitlines()
        )
        assert len(event_log_lines) == invocation_count

        terminal_summary = render_run_summary_terminal(summary)
        assert (
            "Invocation detail: retained latest "
            f"{MAX_RETAINED_INVOCATION_USAGE_DETAILS} invocation(s); "
            f"{overflow_count} earlier omitted" in terminal_summary
        )


def test_exact_provider_totals_include_all_terminal_events_beyond_detail_cap() -> None:
    workflow = single_node_workflow()
    invocation_count = MAX_RETAINED_INVOCATION_USAGE_DETAILS + 5
    codex_count = sum(index % 3 != 0 for index in range(invocation_count))
    claude_count = invocation_count - codex_count
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
        for index in range(invocation_count):
            provider = "codex" if index % 3 != 0 else "claude"
            input_tokens = 10 if provider == "codex" else 20
            output_tokens = 2 if provider == "codex" else 4
            persistent_logger.record_event(
                make_execution_event(
                    event_type=(
                        EventType.INVOCATION_FAILED
                        if index % 2
                        else EventType.INVOCATION_FINISHED
                    ),
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    node_id="node.a",
                    provider=provider,
                    role=ProviderRole.EXECUTOR,
                    task_id=f"task_{index:04d}",
                    attempt_count=1,
                    cli_captured=True,
                    output_extraction_status="success",
                    provider_usage_status="full",
                    provider_usage_report_count=1,
                    provider_tokens={
                        "input": input_tokens,
                        "cached_input": 3 if provider == "codex" else None,
                        "cache_write": None,
                        "output": output_tokens,
                        "reasoning": None,
                        "total": input_tokens + output_tokens,
                    },
                )
            )
        persistent_logger.record_event(
            make_execution_event(
                event_type=EventType.INVOCATION_FINISHED,
                workflow_name=workflow.name,
                run_id=output.run_id,
                node_id="node.a",
                provider="legacy",
                role=ProviderRole.EXECUTOR,
                task_id="legacy",
                attempt_count=1,
                provider_tokens={"input": 999_999},
            )
        )

        persistent_logger.stop(RunResult(status="succeeded"))

        summary = persistent_logger.refresh_summary(RunResult(status="succeeded"))
        assert summary is not None
        assert summary is not None
        aggregate = summary.provider_token_aggregates.overall
        assert aggregate is not None
        assert aggregate is not None
        assert aggregate.report_count == invocation_count
        assert aggregate.input == codex_count * 10 + claude_count * 20
        assert aggregate.cached_input is None
        assert aggregate.output == codex_count * 2 + claude_count * 4
        assert aggregate.total == codex_count * 12 + claude_count * 24
        assert [
            (item.provider, item.report_count)
            for item in summary.provider_token_aggregates.providers
        ] == [("claude", claude_count), ("codex", codex_count)]
        provider_aggregates = {
            item.provider: item for item in summary.provider_token_aggregates.providers
        }
        assert provider_aggregates["claude"].cached_input is None
        assert provider_aggregates["codex"].cached_input == codex_count * 3
        markdown = render_run_summary_markdown(summary)
        terminal = render_run_summary_terminal(summary)
        assert (
            f"Provider-reported tokens: {aggregate.total:,} across {invocation_count} reports"
            in markdown
        )
        assert (
            f"Provider-reported tokens: {aggregate.total:,} across {invocation_count} reports"
            in terminal
        )
        assert "legacy" not in [
            item.provider for item in summary.provider_token_aggregates.providers
        ]


def test_codex_fixture_totals_render_for_success_and_failure() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "unit"
        / "adapters"
        / "invokers"
        / "cli_invoker"
        / "fixtures"
        / "provider_usage"
        / "codex_24_reports.jsonl"
    )
    usage_accumulator = InvocationUsageAccumulator(
        ProviderKind.CODEX,
        prompt="prompt",
    )
    for line in fixture_path.read_text(encoding="utf-8").splitlines():
        usage_accumulator.record_provider_usage(
            decode_codex_usage(CommandResult(0, line, ""))
        )
    usage = usage_accumulator.build_usage(
        config=AgentConfig(cli_cmd=["codex"]),
        output_extraction_status="success",
    )

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
                task_id="codex_fixture",
                attempt_count=1,
                cli_captured=True,
                output_extraction_status="success",
                provider_usage_status="full",
                provider_usage_report_count=usage.provider_usage_report_count,
                provider_tokens=dict(usage.provider_tokens),
                visible_estimate_tokens=1,
                visible_estimate_method="char-count-lower-bound",
                visible_estimate_is_lower_bound=True,
            )
        )

        persistent_logger.stop(RunResult(status="succeeded"))
        succeeded_summary = persistent_logger.refresh_summary(
            RunResult(status="succeeded")
        )
        failed_summary = persistent_logger.refresh_summary(RunResult(status="failed"))

        assert succeeded_summary is not None
        assert failed_summary is not None
        assert succeeded_summary is not None
        assert failed_summary is not None
        assert succeeded_summary.workflow_status == "succeeded"
        assert failed_summary.workflow_status == "failed"
        assert (
            succeeded_summary.provider_token_aggregates
            == failed_summary.provider_token_aggregates
        )

        expected_lines = (
            "Provider-reported tokens: 7,598,612 across 24 reports",
            "Input: 7,359,384 (cached input: 6,124,416)",
            "Output: 239,228 (reasoning output: 108,672)",
        )
        for summary in (succeeded_summary, failed_summary):
            markdown = render_run_summary_markdown(summary)
            terminal = render_run_summary_terminal(summary)
            for expected_line in expected_lines:
                assert expected_line in markdown
                assert expected_line in terminal
            assert "Provider `codex`" in markdown
            assert "Provider codex" in terminal
            assert "Cache write:" not in markdown
            assert "Cache write:" not in terminal


def test_standalone_cached_input_total_renders_in_both_formats() -> None:
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
                task_id="cached-input-only",
                attempt_count=1,
                provider_usage_report_count=1,
                provider_tokens={"cached_input": 42},
            )
        )
        persistent_logger.stop(RunResult(status="succeeded"))

        summary = persistent_logger.refresh_summary(RunResult(status="succeeded"))
        assert summary is not None
        assert summary is not None
        markdown = render_run_summary_markdown(summary)
        terminal = render_run_summary_terminal(summary)

        assert "Cached input: 42" in markdown
        assert "Cached input: 42" in terminal
