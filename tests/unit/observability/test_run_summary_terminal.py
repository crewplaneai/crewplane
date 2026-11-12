from __future__ import annotations

import re
from dataclasses import replace
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path

import pytest
from rich.console import Console, Group

from crewplane.observability.run_summary.models import (
    InvocationTimingSummary,
    IssueSummary,
    NodeCounts,
    NodeOutcomeSummary,
    ProviderTokenAggregate,
    ProviderTokenAggregates,
    ProviderUsageRollup,
    RunSummary,
    SpendTotals,
)
from crewplane.observability.run_summary.terminal import render_run_summary_terminal
from crewplane.observability.text_layout import display_width


def _summary(root: Path) -> RunSummary:
    return RunSummary(
        workflow_name="Review project",
        run_id="example-run",
        workflow_status="succeeded",
        review_consensus_unresolved=True,
        started_at="2026-11-10T15:00:00Z",
        completed_at="2026-11-10T15:01:24Z",
        elapsed_seconds=84.0,
        node_counts=NodeCounts(0, 0, 3, 0, 0, 0),
        invocation_count=2,
        invocation_timings=(
            InvocationTimingSummary(
                "review", "executor", "codex", "model-a", None, 1, "succeeded", 80_125
            ),
            InvocationTimingSummary(
                "review", "reviewer", "claude", "model-b", None, 1, "succeeded", 50_500
            ),
        ),
        spend=SpendTotals(2, 3, 2, 2, 0, 0, 100, 0.042, "full"),
        provider_rollups=(),
        invocation_usages=(),
        omitted_invocation_usage_count=0,
        node_outcomes=(
            NodeOutcomeSummary(
                "context", "succeeded", None, root / "context-result.md", "reused"
            ),
            NodeOutcomeSummary(
                "review", "succeeded", 80.125, root / "review-result.md", None
            ),
            NodeOutcomeSummary(
                "report", "succeeded", 3.0, root / "report-result.md", None
            ),
        ),
        workspace=None,
        issues=(),
        artifact_references=(),
        event_log_path=root / "logs" / "events.ndjson",
        summary_path=root / "logs" / "summary.md",
        provider_token_aggregates=ProviderTokenAggregates(
            ProviderTokenAggregate(
                None, 3, input=10_000, cached_input=1_000, output=2_400, total=12_400
            ),
            (
                ProviderTokenAggregate(
                    "codex",
                    3,
                    input=10_000,
                    cached_input=1_000,
                    output=2_400,
                    total=12_400,
                ),
            ),
        ),
    )


def _render(summary: RunSummary, width: int = 120) -> str:
    stream = StringIO()
    console = Console(file=stream, width=width, color_system=None, legacy_windows=False)
    rendered = render_run_summary_terminal(summary, console)
    assert isinstance(rendered, Group)
    console.print(rendered, markup=False, highlight=False)
    return stream.getvalue()


@pytest.mark.parametrize("project_name", ["project", "long-project-name-" * 8])
def test_sections_report_execution_review_timing_and_recorded_usage(
    tmp_path: Path, project_name: str
) -> None:
    root = tmp_path / project_name
    output = _render(_summary(root))
    headings = (
        "Run Summary: Review project",
        "Needs attention",
        "Stages\n",
        "Agent call durations",
        "Usage details",
        "Results and reports",
    )
    positions = [output.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "Execution: succeeded" in output
    assert "Review unresolved; execution continued" in output
    assert "Elapsed: 1m24s" in output
    assert "Recorded attempts: 3" in output
    assert "Calls: 2" in output
    assert "Provider-reported tokens: 12,400 across 3 reports" in output
    assert "Configured cost estimate: $0.042000 (full)" in output
    assert "succeeded (reused)" in output
    assert "80.125s" in output and "50.500s" in output
    assert "durations may overlap" in output
    assert "not workflow elapsed time" in output
    assert output.index("80.125s") < output.index("50.500s")
    compact = output.replace("\n", "")
    assert str(root / "report-result.md") in compact
    assert str(root / "logs" / "summary.md") in compact
    assert str(root / "logs" / "events.ndjson") in compact
    assert all(display_width(line) <= 120 for line in output.splitlines())


@pytest.mark.parametrize("width", [120, 80, 40, 24])
def test_unicode_labels_do_not_displace_status_duration_or_paths(
    tmp_path: Path, width: int
) -> None:
    summary = _summary(tmp_path)
    long_label = "界e\u0301🙂[bold]`label`" * 12
    summary = replace(
        summary,
        workflow_name=long_label,
        node_outcomes=(
            NodeOutcomeSummary(
                long_label,
                "cancelled",
                12.345,
                tmp_path / "界 folder" / "actual report.md",
                "checkpoint",
            ),
        ),
        invocation_timings=(
            InvocationTimingSummary(
                long_label,
                long_label,
                "provider",
                "界🙂" * 20,
                2,
                3,
                "cancelled",
                12_345,
            ),
        ),
    )
    output = _render(summary, width)
    assert all(display_width(line) <= width for line in output.splitlines())
    assert "cancelled" in output
    assert "12.3s" in output and "12.345s" in output
    assert "[bold]" in output
    compact = output.replace("\n", "")
    assert str(tmp_path / "界 folder" / "actual report.md") in compact
    assert str(summary.summary_path) in compact
    assert "#" not in output  # A single call does not need a comparison bar.


def test_caps_keep_failure_stages_and_disclose_all_display_omissions(
    tmp_path: Path,
) -> None:
    base = _summary(tmp_path)
    nodes = tuple(
        NodeOutcomeSummary(
            f"stage-{i:02}",
            "failed" if i >= 14 else "succeeded",
            float(i),
            tmp_path / f"result-{i}.md",
            None,
        )
        for i in range(16)
    )
    timings = tuple(
        InvocationTimingSummary(
            f"stage-{i:02}", "task", "codex", "model", None, None, "succeeded", i * 1000
        )
        for i in range(11)
    )
    summary = replace(
        base,
        node_outcomes=nodes,
        invocation_timings=timings,
        invocation_count=99,
        issues=tuple(IssueSummary("warning", "n/a", f"problem-{i}") for i in range(5)),
    )
    output = _render(summary)
    assert "stage-14" in output and "stage-15" in output
    assert "4 more stages in summary.md" in output
    assert "3 more agent calls in summary.md" in output
    assert "2 more retained issues in summary.md" in output
    assert "13 more result paths in summary.md" in output
    assert "Calls: 99" in output
    assert "Provider-reported tokens: 12,400 across 3 reports" in output
    stages = output.split("Stages\n", 1)[1].split("Agent call durations", 1)[0]
    assert (
        stages.index("stage-00") < stages.index("stage-14") < stages.index("stage-15")
    )
    assert "stage-10" not in stages


def test_errors_precede_retention_warning_and_long_issue_is_bounded(
    tmp_path: Path,
) -> None:
    summary = replace(
        _summary(tmp_path),
        issues=(
            IssueSummary("error", "now", "actual failure " * 100),
            IssueSummary("error", "now", "second failure"),
            IssueSummary("warning", "now", "first warning"),
            IssueSummary(
                "warning",
                "n/a",
                "[warning] Summary detail retained the latest 2 event(s); 9 earlier omitted",
            ),
        ),
    )
    output = _render(summary, 80)
    attention = output.split("Needs attention\n", 1)[1].split("Stages\n", 1)[0]
    assert (
        attention.index("actual failure")
        < attention.index("second failure")
        < attention.index("first warning")
    )
    assert "Summary detail retained" not in attention
    assert "1 more retained issues in summary.md" in attention
    assert "Full issue in summary.md" in attention
    assert len(attention.splitlines()) < 20


@pytest.mark.parametrize("duration", [None, 0])
def test_missing_and_zero_timings_estimates_and_cost_are_distinct(
    tmp_path: Path, duration: int | None
) -> None:
    base = _summary(tmp_path)
    summary = replace(
        base,
        elapsed_seconds=None,
        node_counts=None,
        invocation_count=None,
        invocation_timings=(
            replace(base.invocation_timings[0], duration_ms=duration, model=None),
        ),
        spend=SpendTotals(
            1, 2, 0, 0, 0, 0, duration, 0.0 if duration == 0 else None, "none"
        ),
        provider_token_aggregates=ProviderTokenAggregates(),
        node_outcomes=(),
    )
    output = _render(summary)
    assert "Elapsed: n/a" in output and "Stages: n/a" in output
    assert "Calls: n/a" in output
    assert "Provider-reported tokens: n/a" in output
    assert "model n/a" in output
    assert ("0.000s" in output) is (duration == 0)
    assert ("$0.000000" in output) is (duration == 0)
    assert (
        "Visible-text estimate: "
        + ("0 tokens (lower bound)" if duration == 0 else "n/a")
        in output
    )
    assert "Recorded attempts: 2" in output


def test_known_zero_calls_and_no_telemetry_do_not_invent_measurements(
    tmp_path: Path,
) -> None:
    summary = replace(
        _summary(tmp_path),
        spend=None,
        invocation_count=0,
        invocation_timings=(),
        provider_token_aggregates=ProviderTokenAggregates(),
    )
    output = _render(summary)
    assert "Calls: 0 (no new calls)" in output
    assert "Recorded attempts: n/a" in output
    assert "Provider-reported tokens: n/a" in output
    assert "Configured cost estimate" not in output


def test_provider_limit_keeps_buckets_separate_from_report_counts(
    tmp_path: Path,
) -> None:
    providers = tuple(
        ProviderTokenAggregate(
            f"provider-{i}",
            7,
            input=10,
            cached_input=3,
            cache_write=4,
            output=11,
            reasoning=5,
            total=None,
        )
        for i in range(6)
    )
    rollups = tuple(
        ProviderUsageRollup(f"provider-{i}", 2, 3, 1, 1, 0, 1, None, None, "none")
        for i in range(6)
    )
    summary = replace(
        _summary(tmp_path),
        provider_token_aggregates=ProviderTokenAggregates(providers=providers),
        provider_rollups=rollups,
        omitted_invocation_usage_count=20,
    )
    output = _render(summary)
    assert "2 more providers in summary.md" in output
    assert "provider-3" in output and "provider-4" not in output
    assert "Reports: 7" in output
    assert "input=10" in output and "cached input=3" in output
    assert (
        "output=11" in output and "reasoning=5" in output and "cache write=4" in output
    )
    assert "total=n/a" in output
    assert "20 earlier usage rows not retained" in output
    assert "events.ndjson" in output


class _TTYStream(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    "mode",
    [
        "tty",
        "no-color",
        "dumb",
        "redirected",
        "force-color",
        "tty-compatible",
        "legacy-windows",
    ],
)
def test_styles_require_physical_tty_and_color_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("TTY_COMPATIBLE", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = (
        _TTYStream()
        if mode in {"tty", "no-color", "dumb", "legacy-windows"}
        else StringIO()
    )
    if mode == "no-color":
        monkeypatch.setenv("NO_COLOR", "1")
    if mode == "dumb":
        monkeypatch.setenv("TERM", "dumb")
    if mode == "force-color":
        monkeypatch.setenv("FORCE_COLOR", "1")
    if mode == "tty-compatible":
        monkeypatch.setenv("TTY_COMPATIBLE", "1")
    console = Console(file=stream, width=80, legacy_windows=mode == "legacy-windows")
    summary = replace(
        _summary(tmp_path),
        workflow_name="[bold]literal[/bold]\x1b[31mBAD\x1b[0m",
        issues=(
            IssueSummary(
                "error",
                "now",
                "error\x1b]8;;https://evil.test\x07link\x1b]8;;\x07\x07\x9b31m",
            ),
        ),
    )
    console.print(
        render_run_summary_terminal(summary, console), markup=False, highlight=False
    )
    output = stream.getvalue()
    assert "[bold]literal[/bold]BAD" in output
    assert "\x1b]" not in output and "\x07" not in output and "\x9b" not in output
    if mode not in {"tty", "legacy-windows"}:
        assert "\x1b" not in output
    if mode == "tty":
        assert "\x1b[" in output


def test_strict_ascii_stream_writes_escaped_unicode_without_changing_console(
    tmp_path: Path,
) -> None:
    raw = BytesIO()
    with TextIOWrapper(
        raw, encoding="ascii", errors="strict", write_through=True
    ) as stream:
        console = Console(
            file=stream, width=40, force_terminal=True, legacy_windows=True
        )
        before = (
            console.width,
            console.encoding,
            console.no_color,
            console.is_terminal,
        )
        summary = replace(
            _summary(tmp_path),
            workflow_name="界🙂e\u0301",
            summary_path=tmp_path / "界" / "summary.md",
        )
        console.print(
            render_run_summary_terminal(summary, console), markup=False, highlight=False
        )
        output = raw.getvalue().decode("ascii")
        assert "\\u754c" in output and "\\U0001f642" in output and "\\u0301" in output
        assert "\x1b" not in output
        assert before == (
            console.width,
            console.encoding,
            console.no_color,
            console.is_terminal,
        )
        assert all(display_width(line) <= 40 for line in output.splitlines())


@pytest.mark.parametrize("width", [120, 80, 40, 24])
def test_comparison_bars_disappear_before_status_and_exact_values(
    tmp_path: Path, width: int
) -> None:
    output = _render(_summary(tmp_path), width)
    assert ("#" in output) is (width >= 60)
    assert "80.125s" in output and "50.500s" in output
    assert "succeeded" in output
    assert all(display_width(line) <= width for line in output.splitlines())


def test_timing_ties_have_stable_identity_order_and_unknown_is_last(
    tmp_path: Path,
) -> None:
    summary = _summary(tmp_path)
    timing = summary.invocation_timings[0]
    summary = replace(
        summary,
        invocation_timings=(
            replace(timing, node_id="zulu", duration_ms=1000),
            replace(timing, node_id="alpha", duration_ms=None),
            replace(timing, node_id="beta", duration_ms=1000),
            replace(timing, node_id="beta", task_id="aaa", duration_ms=1000),
        ),
    )
    output = (
        _render(summary)
        .split("Agent call durations\n", 1)[1]
        .split("Usage details", 1)[0]
    )
    assert (
        output.index("beta / aaa")
        < output.index("beta / executor")
        < output.index("zulu / executor")
        < output.index("alpha / executor")
    )
    assert "n/a" in output


@pytest.mark.parametrize("width", [23, 24])
def test_long_labels_and_issues_remain_bounded_in_stacked_layout(
    tmp_path: Path, width: int
) -> None:
    summary = _summary(tmp_path)
    summary = replace(
        summary,
        workflow_name="x" * 10_000,
        provider_token_aggregates=ProviderTokenAggregates(
            providers=(ProviderTokenAggregate("p" * 10_000, 1),)
        ),
        node_outcomes=(replace(summary.node_outcomes[0], node_id="y" * 10_000),),
        invocation_timings=(
            replace(
                summary.invocation_timings[0], node_id="z" * 10_000, model="q" * 10_000
            ),
        ),
        issues=(IssueSummary("error", "now", "failure " * 10_000),),
    )
    output = _render(summary, width)
    assert len(output.splitlines()) < 100
    assert "succeeded (reused)" in output
    assert "80.125s" in output
    assert "Full issue in summary.md" in " ".join(output.split())
    assert str(summary.summary_path) in output.replace("\n", "")
    assert all(display_width(line) <= width for line in output.splitlines())


def test_label_linebreaks_and_control_characters_are_normalized(tmp_path: Path) -> None:
    summary = replace(
        _summary(tmp_path), workflow_name="one\r\ntwo\u2028three\u2029four\tfive\x00six"
    )
    output = _render(summary)
    assert "Run Summary: one two three four fivesix" in output
    assert not any(
        character in output for character in ("\r", "\t", "\x00", "\u2028", "\u2029")
    )


@pytest.mark.parametrize(
    "legacy_windows", [False, True], ids=["modern", "legacy-windows"]
)
@pytest.mark.parametrize("width", [39, 40, 41, 59, 60, 61, 79, 80, 81, 120])
def test_duration_layout_thresholds_preserve_proportional_and_missing_values(
    tmp_path: Path, width: int, legacy_windows: bool
) -> None:
    summary = _summary(tmp_path)
    summary = replace(
        summary,
        invocation_timings=tuple(
            replace(summary.invocation_timings[0], node_id=node, duration_ms=duration)
            for node, duration in (
                ("longest", 1000),
                ("half", 500),
                ("tiny", 1),
                ("zero", 0),
                ("missing", None),
            )
        ),
    )
    stream = StringIO()
    console = Console(
        file=stream,
        width=width,
        height=24,
        color_system=None,
        legacy_windows=legacy_windows,
    )

    console.print(
        render_run_summary_terminal(summary, console), markup=False, highlight=False
    )
    output = stream.getvalue()
    durations = output.split("Agent call durations\n", 1)[1].split("Usage details", 1)[
        0
    ]

    effective_width = console.width
    assert ("Provider /" in durations) is (effective_width >= 80)
    assert ("Call / model" in durations) is (40 <= effective_width < 80)
    for value in ("1.000s", "0.500s", "0.001s", "0.000s", "n/a"):
        assert value in durations
    bar_width = min(20, effective_width // 5)
    assert [len(bar) for bar in re.findall(r"#+", durations)] == (
        [bar_width, round(bar_width / 2), 1] if effective_width >= 60 else []
    )
    assert all(display_width(line) <= effective_width for line in output.splitlines())


@pytest.mark.parametrize(
    ("durations", "bar_sizes"),
    [
        ((None, None), []),
        ((0, 0), []),
        ((1000, None), []),
        ((1000, 0), [20]),
    ],
)
def test_comparison_bars_require_positive_maximum_and_two_recorded_durations(
    tmp_path: Path, durations: tuple[int | None, ...], bar_sizes: list[int]
) -> None:
    summary = _summary(tmp_path)
    summary = replace(
        summary,
        invocation_timings=tuple(
            replace(timing, duration_ms=duration)
            for timing, duration in zip(
                summary.invocation_timings, durations, strict=True
            )
        ),
    )

    output = _render(summary)

    assert [len(bar) for bar in re.findall(r"#+", output)] == bar_sizes


@pytest.mark.parametrize("has_spend", [False, True])
def test_provider_union_preserves_order_latest_aggregate_and_optional_details(
    tmp_path: Path, has_spend: bool
) -> None:
    summary = _summary(tmp_path)
    summary = replace(
        summary,
        spend=summary.spend if has_spend else None,
        provider_token_aggregates=ProviderTokenAggregates(
            providers=(
                ProviderTokenAggregate("zeta", 11),
                ProviderTokenAggregate(None, 2),
                ProviderTokenAggregate("", 3),
                ProviderTokenAggregate("zeta", 7),
            )
        ),
        provider_rollups=tuple(
            ProviderUsageRollup(provider, calls, 0, 0, 0, 0, 0, None, None, "none")
            for provider, calls in (("", 5), ("alpha", 6), ("beta", 99))
        ),
        omitted_invocation_usage_count=20,
    )

    output = _render(summary, 240)
    usage = output.split("Usage details\n", 1)[1].split("Results and reports", 1)[0]
    provider_lines = [
        line
        for line in usage.splitlines()
        if line in {"zeta", "unknown provider", "alpha", "beta"}
    ]

    assert provider_lines == ["zeta", "unknown provider", "unknown provider", "alpha"]
    assert ("Visible-text estimate: 100 tokens (lower bound)" in usage) is has_spend
    assert "Reports: 7;" in usage and "Reports: 11;" not in usage
    assert "Reports: 2;" in usage and "Reports: 3;" in usage
    assert "5 calls with usage facts" in usage and "6 calls with usage facts" in usage
    assert "99 calls with usage facts" not in usage
    assert usage.index("1 more providers in summary.md") < usage.index(
        "20 earlier usage rows not retained"
    )
