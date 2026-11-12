from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.architecture.contracts import LogLevel
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    EventType,
    ExecutionEventContext,
    invocation_event,
    runtime_log_event,
)
from crewplane.observability.run_summary.formatting import (
    invocation_label,
)
from crewplane.observability.run_summary.issues import issue_summaries
from crewplane.observability.run_summary.markdown import render_run_summary_markdown
from crewplane.observability.run_summary.models import (
    InvocationUsageSummary,
    NodeCounts,
    ProviderTokenAggregate,
    ProviderTokenAggregates,
    ProviderUsageRollup,
    RunSummary,
    SpendTotals,
    WorkspaceInvocationSummary,
    WorkspaceRunSummary,
)
from crewplane.observability.run_summary.workspace import workspace_plan_summary
from tests.helpers.observability import render_summary_terminal


def _empty_summary(root: Path) -> RunSummary:
    return RunSummary(
        workflow_name="flow",
        run_id="run",
        workflow_status="failed",
        review_consensus_unresolved=False,
        started_at="unknown",
        completed_at="unknown",
        elapsed_seconds=None,
        invocation_count=None,
        invocation_timings=(),
        node_counts=NodeCounts(0, 0, 0, 0, 0, 0),
        spend=None,
        provider_rollups=(),
        invocation_usages=(),
        omitted_invocation_usage_count=0,
        node_outcomes=(),
        workspace=None,
        issues=(),
        artifact_references=(),
        event_log_path=root / "events.ndjson",
        summary_path=root / "summary.md",
    )


@pytest.mark.parametrize("contract_mode", [None, "blob_exact"])
def test_workspace_plan_without_optional_metadata_is_still_rendered(
    tmp_path: Path, contract_mode: str | None
) -> None:
    preflight = tmp_path / "preflight"
    preflight.mkdir()
    (preflight / "manifest.json").write_text(
        json.dumps({"workspace": {"worktree_contract": {"mode": contract_mode}}}),
        encoding="utf-8",
    )
    plan = workspace_plan_summary(tmp_path)
    assert plan is not None
    summary = replace(_empty_summary(tmp_path), workspace=WorkspaceRunSummary(plan, ()))

    markdown = render_run_summary_markdown(summary)
    terminal = render_summary_terminal(summary)

    assert "No workspace invocations or blob-only inputs recorded." in markdown
    assert "Workspace details in summary.md" in terminal
    assert "rendered workspace files=" not in markdown
    assert (r"contract=blob\_exact" in markdown) is (contract_mode is not None)


def test_workspace_summary_without_plan_keeps_empty_invocation_notice(
    tmp_path: Path,
) -> None:
    summary = replace(_empty_summary(tmp_path), workspace=WorkspaceRunSummary(None, ()))

    assert "No workspace invocations" in render_run_summary_markdown(summary)
    assert "Workspace details in summary.md" in render_summary_terminal(summary)


def test_zero_invocation_spend_has_no_provider_breakdown(tmp_path: Path) -> None:
    spend = SpendTotals(0, 0, 0, 0, 0, 0, 0, None, "none")
    summary = replace(_empty_summary(tmp_path), spend=spend)

    terminal = render_summary_terminal(summary)

    assert "Recorded attempts: 0" in terminal
    assert "  Providers:" not in terminal


def test_terminal_spend_lines_preserves_provider_record_order_and_format(
    tmp_path: Path,
) -> None:
    summary = replace(
        _empty_summary(tmp_path),
        spend=SpendTotals(3, 4, 2, 1, 1, 1, 1_234, None, "mixed"),
        provider_token_aggregates=ProviderTokenAggregates(
            overall=ProviderTokenAggregate(None, 3, input=30, total=40),
            providers=(
                ProviderTokenAggregate("alpha", 2, input=20, total=25),
                ProviderTokenAggregate("beta", 1, output=10, total=15),
            ),
        ),
        provider_rollups=(
            ProviderUsageRollup("alpha", 2, 3, 1, 1, 1, 0, 1_000, None, "none"),
            ProviderUsageRollup("beta", 1, 1, 1, 0, 0, 1, 234, 0.5, "full"),
        ),
        invocation_usages=(_invocation_usage("alpha"), _invocation_usage("beta")),
        omitted_invocation_usage_count=1,
    )

    terminal = render_summary_terminal(summary)
    markdown = render_run_summary_markdown(summary)
    assert "Provider-reported tokens: 40 across 3 reports" in terminal
    assert terminal.index("alpha") < terminal.index("beta")
    assert "Recorded attempts: 4" in terminal
    assert "1,234" in terminal
    assert "1 earlier invocation detail(s)" in markdown
    assert "| alpha | 2 / 3 | 1 | 1 / 1 / 0 | 1,000 | n/a / none |" in markdown
    assert "| beta | 1 / 1 | 1 | 0 / 0 / 1 | 234 | $0.500000 / full |" in markdown


def _invocation_usage(provider: str) -> InvocationUsageSummary:
    return InvocationUsageSummary(
        provider=provider,
        node_id=None,
        task_id=None,
        audit_round_num=None,
        round_num=None,
        attempt_count=1,
        cli_captured=True,
        output_extraction_status="success",
        provider_usage_status="full",
        provider_usage_report_count=1,
        provider_tokens={},
        visible_estimate_tokens=None,
        visible_estimate_method=None,
        visible_estimate_is_lower_bound=False,
        configured_cost_usd=None,
        invocation_cost_confidence="none",
        usage_parse_error=None,
        failure_kind=None,
        failure_phase=None,
        failure_source=None,
        failure_advice=None,
    )


@pytest.mark.parametrize(
    ("aggregate", "expected"),
    [
        (
            ProviderTokenAggregate("mock", 1, cached_input=3),
            ("Provider-reported tokens: n/a across 1 reports", "cached input=3"),
        ),
        (
            ProviderTokenAggregate("mock", 1, cache_write=4, reasoning=5),
            (
                "Provider-reported tokens: n/a across 1 reports",
                "cache write=4",
                "reasoning=5",
            ),
        ),
    ],
)
def test_partial_token_report_does_not_invent_missing_totals(
    tmp_path: Path, aggregate: ProviderTokenAggregate, expected: tuple[str, ...]
) -> None:
    summary = replace(
        _empty_summary(tmp_path),
        provider_token_aggregates=ProviderTokenAggregates(aggregate, (aggregate,)),
    )
    terminal = render_summary_terminal(summary)
    for fragment in expected:
        assert fragment in terminal
    markdown = render_run_summary_markdown(summary)
    assert "| Provider-reported tokens | n/a across 1 reports |" in markdown
    assert "| mock | 1 | n/a |" in markdown


@pytest.mark.parametrize(
    ("node", "task", "audit", "round_num", "expected"),
    [
        (None, None, None, None, "`unknown invocation`"),
        (None, "task", 2, None, "`task` / `audit2`"),
        ("node", None, None, 3, "`node` / `round3`"),
        (None, "task", 2, 3, "`task` / `audit2/round3`"),
    ],
)
def test_invocation_labels_preserve_available_identity(
    node: str | None,
    task: str | None,
    audit: int | None,
    round_num: int | None,
    expected: str,
) -> None:
    assert invocation_label(node, task, audit, round_num) == expected


@pytest.mark.parametrize(
    "audit, round_num, label",
    [
        (None, None, None),
        (2, None, "audit2"),
        (None, 3, "round3"),
        (2, 3, "audit2/round3"),
        (0, None, "audit0"),
        (None, 0, "round0"),
        (0, 0, "audit0/round0"),
    ],
)
def test_round_labels_preserve_exact_rendered_and_issue_strings(
    tmp_path: Path, audit: int | None, round_num: int | None, label: str | None
) -> None:
    invocation = WorkspaceInvocationSummary(
        node_id="node",
        task_id="task",
        audit_round_num=audit,
        round_num=round_num,
        workspace_kind="snapshot",
        logical_worktree_name=None,
        status="succeeded",
        state_path=None,
        writable=None,
        lineage_producer=None,
        child_environment_required=None,
        child_environment_applied=None,
    )
    summary = replace(
        _empty_summary(tmp_path), workspace=WorkspaceRunSummary(None, (invocation,))
    )
    invocation_identity = "`node` / `task`" + (
        f" / `{label}`" if label is not None else ""
    )

    markdown = render_run_summary_markdown(summary)
    assert "kind=snapshot; status=succeeded" in markdown
    assert invocation_identity.replace("`", "\\`") in markdown
    assert "Workspace details in summary.md" in render_summary_terminal(summary)
    context = ExecutionEventContext(
        workflow_name="flow",
        run_id="run",
        node_id="node",
        task_id="task",
        provider="mock",
        role=ProviderRole.EXECUTOR,
        audit_round_num=audit,
        round_num=round_num,
        output_file="output.md",
        log_file="invocation.log",
    )
    warning = runtime_log_event(
        "flow", "run", LogLevel.WARNING, "warning text", "test", context=context
    )
    failure = invocation_event(
        EventType.INVOCATION_FAILED, "flow", "run", context, error="failure text"
    )
    details = (
        "node: node; task: task"
        + (f"; round: {label}" if label is not None else "")
        + "; output: output.md; log: invocation.log"
    )

    assert [issue.message for issue in issue_summaries([warning, failure])] == [
        f"[error] Invocation failed: failure text ({details})",
        f"[warning] warning text ({details})",
    ]
