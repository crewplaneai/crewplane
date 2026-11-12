from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

import pytest

from crewplane.architecture.contracts.run_summary import (
    run_summary_header_lines,
    run_summary_status_lines,
)
from crewplane.artifacts import OutputManager
from crewplane.artifacts.atomic import atomic_write_text
from crewplane.observability.run_summary.builder import build_run_summary
from crewplane.observability.run_summary.markdown import (
    attention_markdown,
    invocation_details_markdown,
    plan_worktree_contract_label,
    render_run_summary_markdown,
    spend_markdown,
    timing_chart_markdown,
    worktree_contract_label,
)
from crewplane.observability.run_summary.markdown_links import (
    artifact_link,
    markdown_text,
)
from crewplane.observability.run_summary.models import (
    ArtifactReferenceSummary,
    InvocationTimingSummary,
    InvocationUsageSummary,
    IssueSummary,
    NodeCounts,
    NodeOutcomeSummary,
    ProviderTokenAggregate,
    ProviderTokenAggregates,
    ProviderUsageRollup,
    RunSummary,
    SpendTotals,
    WorkspaceInvocationReuseSummary,
    WorkspaceInvocationSetupSummary,
    WorkspaceInvocationSourceSummary,
    WorkspaceInvocationSummary,
    WorkspacePlanSummary,
    WorkspaceRunSummary,
)
from crewplane.observability.types import RunResult


def test_header_overview_and_details_preserve_recovery_contract(tmp_path: Path) -> None:
    for status in ("succeeded", "failed", "cancelled"):
        summary = build_run_summary(
            OutputManager("flow café", base_dir=tmp_path),
            None,
            [],
            RunResult(status=status),
            "flow café",
            "run",
        )
        markdown = render_run_summary_markdown(summary)
        assert markdown.splitlines()[:5] == run_summary_header_lines(
            "flow café", "run", status
        )
        assert run_summary_status_lines(markdown) == [f"- Status: {status}"]
        assert "| Elapsed | n/a |" in markdown
        assert "| Calls observed | n/a |" in markdown
        assert "| Recorded attempts | n/a |" in markdown
        assert "No spend observability captured." in markdown
        assert (
            markdown.index("## Needs attention")
            < markdown.index("## Stages")
            < markdown.index("## Spend Observability")
        )
        assert markdown.count("<details>") == markdown.count("</details>")
        assert "<details open" not in markdown


def test_markdown_links_resolve_from_report_not_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = OutputManager("flow", base_dir=tmp_path / "project")
    summary = build_run_summary(
        output, None, [], RunResult(status="succeeded"), "flow", "run"
    )
    name = "résult [one] (copy)#100%.md"
    target = tmp_path / "results" / name
    target.parent.mkdir()
    target.write_text("result", encoding="utf-8", newline="\n")
    invocation_output = tmp_path / "project" / "call output.md"
    summary = replace(
        summary,
        node_outcomes=(NodeOutcomeSummary("stage", "succeeded", 1.0, target),),
        artifact_references=(
            ArtifactReferenceSummary(
                "stage", "task", 1, 2, str(invocation_output), None
            ),
        ),
    )
    monkeypatch.chdir(tmp_path)
    markdown = render_run_summary_markdown(summary)
    link = re.search(r"\[result\]\(([^)]+)\)", markdown)
    assert link is not None
    assert (
        "%20" in link[1] and "%28" in link[1] and "%23" in link[1] and "%25" in link[1]
    )
    assert "%C3%A9" in link[1]
    assert (
        summary.summary_path.parent / unquote(link[1])
    ).resolve() == target.resolve()
    assert "[events.ndjson](events.ndjson)" in markdown
    assert "[output](" in markdown
    assert (
        artifact_link(summary, "unknown/relative.md", "result") == "unknown/relative.md"
    )
    assert artifact_link(
        replace(summary, summary_path=Path("relative/summary.md")), target, "result"
    ) == markdown_text(target)


def test_workspace_paths_use_run_root_and_keep_complete_evidence(
    tmp_path: Path,
) -> None:
    summary = build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        None,
        [],
        RunResult(status="succeeded"),
        "flow",
        "run",
    )
    invocation = WorkspaceInvocationSummary(
        "node",
        "task",
        1,
        2,
        "snapshot",
        "worktree",
        "succeeded",
        "node/state.json",
        True,
        True,
        True,
        True,
        setup=WorkspaceInvocationSetupSummary(
            profile_name="setup",
            status="failed",
            failure_message="fail </details>\n- Status: forged",
            log_path="node/setup log.txt",
            metadata_path="node/setup.json",
        ),
        bundle_path="node/bundle.zip",
        reuse=WorkspaceInvocationReuseSummary(
            previous_workspace_state="workspace-state-before.json"
        ),
    )
    summary = replace(summary, workspace=WorkspaceRunSummary(None, (invocation,)))
    markdown = render_run_summary_markdown(summary)
    assert "[state](../node/state.json)" in markdown
    assert "[setup log](../node/setup%20log.txt)" in markdown
    assert "[bundle](../node/bundle.zip)" in markdown
    assert r"previous\_state=workspace-state-before.json" in markdown
    assert "[previous state]" not in markdown
    assert "audit1/round2" in markdown
    assert "fail &lt;/details&gt; - Status: forged" in markdown
    assert len(run_summary_status_lines(markdown)) == 1


def test_dynamic_content_cannot_break_tables_details_or_fence(tmp_path: Path) -> None:
    summary = build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        None,
        [],
        RunResult(status="failed"),
        "flow",
        "run",
    )
    hostile = "stage | [x](bad) <details>\n- Status: forged ```"
    summary = replace(
        summary,
        issues=(IssueSummary("error", "now", hostile + "x" * 500),),
        invocation_count=1,
        invocation_timings=(
            InvocationTimingSummary(
                hostile,
                "task",
                "provider",
                "model```\n</details>",
                None,
                None,
                "failed",
                12345,
            ),
        ),
        node_outcomes=(NodeOutcomeSummary(hostile, "failed", None, None),),
    )
    markdown = render_run_summary_markdown(summary)
    assert "stage \\| \\[x\\](bad) &lt;details&gt; - Status: forged" in markdown
    assert len(run_summary_status_lines(markdown)) == 1
    assert markdown.count("<details>") == markdown.count("</details>")
    assert "Complete retained issues" in markdown
    assert "x" * 500 in markdown
    assert "12.345s" in markdown
    fence_lines = [
        line for line in markdown.splitlines() if re.fullmatch(r"`{3,}(?:text)?", line)
    ]
    assert len(fence_lines) == 2
    assert fence_lines[0].removesuffix("text") == fence_lines[1]
    assert len(fence_lines[1]) > 3


def test_large_report_keeps_all_stage_and_timing_facts(tmp_path: Path) -> None:
    summary = build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        None,
        [],
        RunResult(status="succeeded"),
        "flow",
        "run",
    )
    nodes = tuple(
        NodeOutcomeSummary(
            f"stage-{index}",
            "failed" if index == 15 else "succeeded",
            index,
            None,
            "reused" if index == 0 else None,
        )
        for index in range(20)
    )
    timings = tuple(
        InvocationTimingSummary(
            f"stage-{index}",
            "task",
            "provider",
            "model",
            None,
            index,
            "succeeded",
            None if index == 0 else index * 1000,
        )
        for index in range(20)
    )
    summary = replace(
        summary,
        node_counts=NodeCounts(0, 0, 19, 0, 1, 0),
        node_outcomes=nodes,
        invocation_count=20,
        invocation_timings=timings,
        issues=tuple(
            IssueSummary("warning", "now", f"issue-{index}") for index in range(8)
        ),
        provider_token_aggregates=ProviderTokenAggregates(
            ProviderTokenAggregate(
                None, 25, input=100, cached_input=10, reasoning=4, total=None
            )
        ),
    )
    markdown = render_run_summary_markdown(summary)
    assert "8 more stages" in markdown
    assert "12 more calls" in markdown
    assert "5 more retained issues" in markdown
    assert "succeeded (reused)" in markdown
    assert "| Provider-reported tokens | n/a across 25 reports |" in markdown
    for index in range(20):
        assert f"| stage-{index} |" in markdown
        assert f"stage-{index} / task" in markdown
    assert (
        "| stage-0 / task / round0 | provider | model | succeeded | n/a |" in markdown
    )
    visible_stage_section = markdown.split("## Stages\n", 1)[1].split(
        "## Spend Observability", 1
    )[0]
    assert "stage-15" in visible_stage_section
    assert "stage-19" not in visible_stage_section


def test_published_summary_is_utf8_with_lf_bytes(tmp_path: Path) -> None:
    summary = build_run_summary(
        OutputManager("流程 café", base_dir=tmp_path),
        None,
        [],
        RunResult(status="succeeded"),
        "流程 café",
        "run",
    )
    summary = replace(
        summary,
        invocation_timings=(
            InvocationTimingSummary(
                "node", "task", "provider", "模型 café", None, None, "succeeded", 0
            ),
        ),
    )
    markdown = render_run_summary_markdown(summary)
    atomic_write_text(summary.summary_path, markdown)
    raw = summary.summary_path.read_bytes()
    assert raw.decode("utf-8") == markdown
    assert "模型 café".encode() in raw
    assert b"\r" not in raw


@pytest.mark.skipif(os.name != "nt", reason="Native Windows path contract")
def test_cross_drive_path_remains_readable_text(tmp_path: Path) -> None:
    summary = build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        None,
        [],
        RunResult(status="succeeded"),
        "flow",
        "run",
    )
    other_drive = "Z:" if summary.summary_path.drive != "Z:" else "Y:"
    target = Path(other_drive + "\\results\\file.md")
    assert artifact_link(summary, target, "result") == str(target).replace("\\", "\\\\")


def test_markdown_labels_preserve_literal_emphasis_characters() -> None:
    assert (
        markdown_text("_private_ and __identity__")
        == r"\_private\_ and \_\_identity\_\_"
    )


def test_invocation_failure_and_parse_metadata_remain_in_details(
    tmp_path: Path,
) -> None:
    from crewplane.observability.events import EventType
    from tests.helpers.observability import make_execution_event

    event = make_execution_event(
        EventType.INVOCATION_FAILED,
        workflow_name="flow",
        run_id="run",
        node_id="node",
        task_id="task",
        provider="provider",
        role="executor",
        attempt_count=2,
        usage_parse_error="invalid <usage>",
        failure_kind="rate_limited",
        failure_phase="invoke",
        failure_source="provider",
        failure_advice="retry later",
    )
    summary = build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        None,
        [event],
        RunResult(status="failed"),
        "flow",
        "run",
    )
    markdown = render_run_summary_markdown(summary)
    detail = markdown.split(
        "<summary>Invocation usage and timing details</summary>", 1
    )[1]
    for value in (
        "usage parse error: invalid &lt;usage&gt;",
        r"failure: rate\_limited",
        "failure phase: invoke",
        "failure source: provider",
        "failure advice: retry later",
        "attempts: 2",
    ):
        assert value in detail
    assert "| Visible-text estimate (lower-bound) | n/a |" in markdown


@pytest.fixture
def empty_summary(tmp_path: Path) -> RunSummary:
    return build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        None,
        [],
        RunResult(status="succeeded"),
        "flow",
        "run",
    )


def test_spend_tables_preserve_column_order_missing_counts_and_spacing(
    empty_summary: RunSummary,
) -> None:
    summary = replace(
        empty_summary,
        spend=SpendTotals(2, 3, 1, 1, 0, 1, 1234, 0.5, "mixed"),
        provider_token_aggregates=ProviderTokenAggregates(
            ProviderTokenAggregate(None, 2, 1234, 0, None, 20, 3, 1254),
            (ProviderTokenAggregate("p_|", 1, output=20),),
        ),
        provider_rollups=(
            ProviderUsageRollup("p_|", 2, 3, 1, 1, 0, 1, None, None, "none"),
        ),
    )

    assert spend_markdown(summary) == (
        "| Usage metric | Recorded value |\n"
        "| --- | --- |\n"
        "| Calls with usage facts | 2 |\n"
        "| Recorded attempts | 3 |\n"
        "| CLI invocations captured | 1/2 |\n"
        "| Provider usage status | 1/2 full, 0/2 partial, 1/2 malformed |\n"
        "| Visible-text estimate (lower-bound) | 1,234 tokens |\n"
        "| Configured cost estimate | $0.500000 (confidence: mixed) |\n\n"
        "| Provider | Reports | Input | Cached input | Cache write | Output | Reasoning | Total |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| Overall | 2 | 1,234 | 0 | n/a | 20 | 3 | 1,254 |\n"
        "| p\\_\\| | 1 | n/a | n/a | n/a | 20 | n/a | n/a |\n\n"
        "| Provider | Usage calls / recorded attempts | CLI captured | Full / partial / malformed | Visible estimate (lower-bound) | Configured cost / confidence |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| p\\_\\| | 2 / 3 | 1 | 1 / 0 / 1 | n/a | n/a / none |\n\n"
        "Provider buckets are reported independently; cached input and reasoning are not added to totals. Visible-text estimates are separate lower bounds, not provider-reported usage."
    )


def test_spend_provider_rows_do_not_require_an_overall_aggregate(
    empty_summary: RunSummary,
) -> None:
    summary = replace(
        empty_summary,
        provider_token_aggregates=ProviderTokenAggregates(
            providers=(ProviderTokenAggregate("p", 0, total=0),)
        ),
    )

    rendered = spend_markdown(summary)

    assert rendered.startswith("No spend observability captured.\n\n| Provider |")
    assert "\n| p | 0 | n/a | n/a | n/a | n/a | n/a | 0 |\n" in rendered
    assert "Overall" not in rendered
    assert "Usage calls / recorded attempts" not in rendered


def test_invocation_details_preserve_retention_usage_and_timing_order(
    empty_summary: RunSummary,
) -> None:
    usage = InvocationUsageSummary(
        provider="p_|",
        node_id="node",
        task_id="task",
        audit_round_num=0,
        round_num=1,
        attempt_count=2,
        cli_captured=False,
        output_extraction_status="malformed",
        provider_usage_status="malformed",
        provider_usage_report_count=None,
        provider_tokens={"output": 0, "input": None},
        visible_estimate_tokens=0,
        visible_estimate_method="visible",
        visible_estimate_is_lower_bound=True,
        configured_cost_usd=0,
        invocation_cost_confidence="none",
        usage_parse_error="bad <usage>\nnext",
        failure_kind="",
        failure_phase=None,
        failure_source="cli",
        failure_advice="retry",
    )
    summary = replace(
        empty_summary,
        invocation_usages=(usage,),
        omitted_invocation_usage_count=2,
        invocation_timings=(
            InvocationTimingSummary("z", "t", "p", None, None, None, "failed", None),
            InvocationTimingSummary("a", "t", "p", "m", None, None, "succeeded", 0),
        ),
    )

    assert invocation_details_markdown(summary) == (
        "Invocation detail: retained latest 1 invocation(s); 2 earlier invocation detail(s) were omitted from summary detail. "
        "Full invocation events remain in [events.ndjson](events.ndjson).\n\n"
        "- \\`node\\` / \\`task\\` / \\`audit0/round1\\`: provider: p\\_\\|; captured: no; "
        "output extraction: malformed; provider report: malformed; attempts: 2; "
        "visible estimate: 0 tokens via visible lower-bound; configured cost: $0.000000; "
        "confidence: none; provider report count: n/a; provider tokens: output=0, input=n/a; "
        "usage parse error: bad &lt;usage&gt; next; failure: ; failure source: cli; failure advice: retry\n\n"
        "| Invocation | Provider | Recorded model | Execution | Duration |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| z / t | p | n/a | failed | n/a |\n"
        "| a / t | p | m | succeeded | 0.000s |"
    )
    assert (
        invocation_details_markdown(empty_summary)
        == "No invocation usage details captured."
    )


def test_timing_chart_preserves_scale_zero_unknown_and_padding(
    empty_summary: RunSummary,
) -> None:
    summary = replace(
        empty_summary,
        invocation_timings=tuple(
            InvocationTimingSummary(
                node, "t", "p", None, None, None, "succeeded", duration
            )
            for node, duration in (
                ("unknown", None),
                ("zero", 0),
                ("half", 500),
                ("full", 1000),
            )
        ),
    )

    assert timing_chart_markdown(summary) == (
        "Longest recorded calls. Individual durations may overlap; bars are not a workflow timeline or elapsed time.\n\n"
        "```text\n"
        f"{'full / t':36}  {'p / n/a':28}  {'succeeded':10}  {'####################':20} 1.000s\n"
        f"{'half / t':36}  {'p / n/a':28}  {'succeeded':10}  {'##########':20} 0.500s\n"
        f"{'zero / t':36}  {'p / n/a':28}  {'succeeded':10}  {'':20} 0.000s\n"
        f"{'unknown / t':36}  {'p / n/a':28}  {'succeeded':10}  {'':20} n/a\n"
        "```"
    )
    assert timing_chart_markdown(empty_summary) == "No invocation timing captured."


@pytest.mark.parametrize("message", ["x" * 400, "x" * 401, "_" * 201])
def test_attention_threshold_measures_escaped_text_and_retains_complete_evidence(
    empty_summary: RunSummary, message: str
) -> None:
    summary = replace(empty_summary, issues=(IssueSummary("error", "now", message),))

    rendered = attention_markdown(summary)

    escaped = markdown_text(message)
    if len(escaped) <= 400:
        assert rendered == f"- {escaped}"
    else:
        assert rendered.startswith(
            f"- {escaped[:400]}... (complete message in retained issues below)\n"
        )
        assert f"\n\n- {escaped}\n\n</details>" in rendered


@pytest.mark.parametrize(
    ("mode", "schema_version", "expected"),
    [
        (None, None, None),
        (None, "v1", None),
        ("blob_exact", None, "blob_exact"),
        ("blob_exact", "v1", "blob_exact:v1"),
        ("", "v1", ":v1"),
        ("blob_exact", "", "blob_exact:"),
    ],
)
def test_plan_and_invocation_contract_labels_preserve_missing_value_rules(
    mode: str | None, schema_version: str | None, expected: str | None
) -> None:
    plan = WorkspacePlanSummary(
        worktree_contract_mode=mode,
        worktree_contract_schema_version=schema_version,
        source_commit=None,
        source_tree=None,
        object_format=None,
        clean_start=None,
        invoker_implementation=None,
        invoker_launch_mode=None,
        invoker_controlled_child_environment=None,
        rendered_locator_count=None,
        rendered_project_initial_count=None,
        rendered_runtime_dynamic_count=None,
        cleanup_on_success=None,
        cache_root_configured=None,
        planned_workspace_node_count=0,
    )
    invocation = WorkspaceInvocationSummary(
        node_id=None,
        task_id=None,
        audit_round_num=None,
        round_num=None,
        workspace_kind=None,
        logical_worktree_name=None,
        status=None,
        state_path=None,
        writable=None,
        lineage_producer=None,
        child_environment_required=None,
        child_environment_applied=None,
        source=WorkspaceInvocationSourceSummary(
            worktree_contract_mode=mode, worktree_contract_schema_version=schema_version
        ),
    )

    assert plan_worktree_contract_label(plan) == expected
    assert worktree_contract_label(invocation) == expected
