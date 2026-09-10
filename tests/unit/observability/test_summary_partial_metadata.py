from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.observability.run_summary.formatting import (
    format_provider_token_aggregate_lines,
    invocation_label,
)
from crewplane.observability.run_summary.markdown import render_run_summary_markdown
from crewplane.observability.run_summary.models import (
    NodeCounts,
    ProviderTokenAggregate,
    RunSummary,
    SpendTotals,
    WorkspaceRunSummary,
)
from crewplane.observability.run_summary.terminal import render_run_summary_terminal
from crewplane.observability.run_summary.workspace import workspace_plan_summary


def _empty_summary(root: Path) -> RunSummary:
    return RunSummary(
        workflow_name="flow",
        run_id="run",
        workflow_status="failed",
        review_consensus_unresolved=False,
        started_at="unknown",
        completed_at="unknown",
        elapsed_label=None,
        node_counts=NodeCounts(0, 0, 0, 0, 0),
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
    terminal = render_run_summary_terminal(summary)

    assert "No workspace invocations or blob-only inputs recorded." in markdown
    assert "No workspace invocations recorded." in terminal
    assert "rendered workspace files=" not in markdown
    assert ("contract=blob_exact" in markdown) is (contract_mode is not None)


def test_workspace_summary_without_plan_keeps_empty_invocation_notice(
    tmp_path: Path,
) -> None:
    summary = replace(_empty_summary(tmp_path), workspace=WorkspaceRunSummary(None, ()))

    assert "No workspace invocations" in render_run_summary_markdown(summary)
    assert "No workspace invocations" in render_run_summary_terminal(summary)


def test_zero_invocation_spend_has_no_provider_breakdown(tmp_path: Path) -> None:
    spend = SpendTotals(0, 0, 0, 0, 0, 0, 0, None, "none")
    summary = replace(_empty_summary(tmp_path), spend=spend)

    terminal = render_run_summary_terminal(summary)

    assert "Spend Observability" in terminal
    assert "  Providers:" not in terminal


@pytest.mark.parametrize(
    ("aggregate", "expected"),
    [
        (
            ProviderTokenAggregate("mock", 1, cached_input=3),
            ("Provider-reported tokens: n/a across 1 reports", "Cached input: 3"),
        ),
        (
            ProviderTokenAggregate("mock", 1, cache_write=4, reasoning=5),
            (
                "Provider-reported tokens: n/a across 1 reports",
                "Cache write: 4",
                "Reasoning output: 5",
            ),
        ),
    ],
)
def test_partial_token_report_does_not_invent_missing_totals(
    aggregate: ProviderTokenAggregate, expected: tuple[str, ...]
) -> None:
    assert format_provider_token_aggregate_lines(aggregate) == expected


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
