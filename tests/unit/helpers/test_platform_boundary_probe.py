from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from crewplane.architecture.contracts import CommandResult
from crewplane.artifacts import OutputManager
from crewplane.observability.run_summary.builder import build_run_summary
from crewplane.observability.run_summary.markdown import render_run_summary_markdown
from crewplane.observability.run_summary.models import (
    ArtifactReferenceSummary,
    InvocationTimingSummary,
    NodeTokenAggregate,
    RunSummary,
)
from crewplane.observability.types import RunResult
from tests.helpers import platform_boundary_probe as probe


def command_result(root: Path, name: str, returncode: int) -> CommandResult:
    stdout = root / f"{name}.stdout"
    stderr = root / f"{name}.stderr"
    stdout.write_bytes(b"exact\r\n\x1a\xff" if returncode == 0 else b"")
    stderr.write_bytes(
        b"diagnostic\n" if returncode == 0 else b"Provider launch failed\r\n"
    )
    return CommandResult(returncode, "", "", stdout, stderr)


@pytest.mark.parametrize("failure_kind", ["exception", "result"])
def test_capture_process_records_missing_command_and_cleans_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    success = command_result(tmp_path, "success", 0)
    launch_error = RuntimeError("Unable to start command")
    launch_error.__cause__ = FileNotFoundError("missing executable")
    runner = AsyncMock(
        side_effect=[
            success,
            launch_error
            if failure_kind == "exception"
            else command_result(tmp_path, "missing", 127),
        ]
    )
    monkeypatch.setattr(probe, "run_command_once", runner)
    (tmp_path / "provider.log").write_bytes(b"header\nexact\r\n\x1a\xffdiagnostic\n")

    captured = asyncio.run(probe.capture_process(tmp_path))

    assert captured["streams"] == {
        "stdout": b"exact\r\n\x1a\xff".hex(),
        "stderr": b"diagnostic\n".hex(),
    }
    assert captured["receipts"] == []
    assert captured["log"] == b"header\nexact\r\n\x1a\xffdiagnostic\n".hex()
    if failure_kind == "exception":
        assert captured["failure"] == {
            "type": "RuntimeError",
            "message": "Unable to start command",
            "cause": "FileNotFoundError",
            "cause_message": "missing executable",
        }
    else:
        assert captured["failure"] == {
            "returncode": 127,
            "stdout": "",
            "stderr": b"Provider launch failed\r\n".hex(),
        }
    assert not list(tmp_path.glob("*.stdout"))
    assert not list(tmp_path.glob("*.stderr"))


def test_capture_process_cleans_unexpected_missing_command_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        probe,
        "run_command_once",
        AsyncMock(
            side_effect=[
                command_result(tmp_path, "success", 0),
                command_result(tmp_path, "missing", 0),
            ]
        ),
    )
    with pytest.raises(AssertionError):
        asyncio.run(probe.capture_process(tmp_path))
    assert not list(tmp_path.glob("*.stdout"))
    assert not list(tmp_path.glob("*.stderr"))


def write_run(project: Path, output: bytes, summary: bytes) -> Path:
    stage = project / ".crewplane/execution-stages/run"
    results = project / ".crewplane/execution-results/run"
    results.mkdir(parents=True, exist_ok=True)
    (results / "result.md").write_bytes(output)
    (results / "report.bin").write_bytes(probe.GENERATED_BYTES)
    for name in ("manifests/nodes", "manifests/review-checkpoints", "logs"):
        (stage / name).mkdir(parents=True, exist_ok=True)
    (stage / "manifests/run.json").write_bytes(
        json.dumps(
            {"status": "succeeded", "run_id": "run", "workflow_signature": "signature"}
        ).encode()
    )
    (stage / "manifests/nodes/node.json").write_bytes(
        json.dumps(
            {
                "status": "succeeded",
                "node_id": "node",
                "artifacts": [
                    {
                        "relative_path": "result.md",
                        "size_bytes": len(output),
                        "sha256": hashlib.sha256(output).hexdigest(),
                    }
                ],
            }
        ).encode()
    )
    (stage / "manifests/review-checkpoints/node.json").write_bytes(b"{}")
    (stage / "logs/events.ndjson").write_bytes(b"")
    (stage / "logs/summary.md").write_bytes(summary)
    return stage


@pytest.mark.parametrize("field", ["results", "summary"])
def test_capture_run_detects_line_ending_changes_with_valid_manifest_hashes(
    tmp_path: Path, field: str
) -> None:
    text = "café\ncontrol-Z\x1a\n".encode()
    summary = b"Parallel:\n- `fanout` / z\n- `fanout` / a\n"
    stage = write_run(tmp_path, text, summary)
    original = probe.capture_run(tmp_path, stage)
    stage = write_run(
        tmp_path,
        text.replace(b"\n", b"\r\n") if field == "results" else text,
        summary.replace(b"\n", b"\r\n") if field == "summary" else summary,
    )

    changed = probe.capture_run(tmp_path, stage)

    assert original[field] != changed[field]
    if field == "results":
        assert changed[field] == {"result.md": text.decode().replace("\n", "\r\n")}
    else:
        assert changed[field] == "Parallel:\r\n- `fanout` / a\r\n- `fanout` / z\r\n"


def test_summary_normalization_preserves_bytes_around_parallel_rows(
    tmp_path: Path,
) -> None:
    text = "Parallel:\r\n- `fanout` / z\r\n- `fanout` / a\r\n\r\nOther\r\n"
    assert probe.normalized_summary(text, tmp_path, "run") == (
        "Parallel:\r\n- `fanout` / a\r\n- `fanout` / z\r\n\r\nOther\r\n"
    )


@pytest.mark.parametrize("default_newline", ["\n", "\r\n"])
def test_probe_writes_lf_json_without_changing_captured_newlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default_newline: str
) -> None:
    project = tmp_path / "project"
    output = tmp_path / "capture.json"
    payload = {"output": "café\r\ncontrol-Z\x1a\n"}
    capture = Mock(return_value=payload)
    write_text = Path.write_text

    def write_with_platform_newline(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        return write_text(
            path,
            data,
            encoding,
            errors,
            default_newline if newline is None else newline,
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        probe.sys,
        "argv",
        ["platform_boundary_probe", "--project", str(project), "--output", str(output)],
    )
    monkeypatch.setattr(probe, "capture", capture)
    monkeypatch.setattr(Path, "write_text", write_with_platform_newline)

    probe.main()

    serialized = output.read_bytes()
    assert b"\r" not in serialized
    assert serialized.endswith(b"\n")
    assert json.loads(serialized) == payload
    capture.assert_called_once_with(project)


def _parallel_summary(tmp_path: Path) -> RunSummary:
    summary = build_run_summary(
        OutputManager("Review café", base_dir=tmp_path),
        None,
        [],
        RunResult(status="succeeded"),
        "Review café",
        "run",
    )
    timings = (
        InvocationTimingSummary(
            "before", "task", "provider", "model café", None, None, "succeeded", 800
        ),
        InvocationTimingSummary(
            "fanout", "alpha-0", "provider", "model café", None, 1, "succeeded", 1234
        ),
        InvocationTimingSummary(
            "fanout", "alpha-1", "provider", "model café", None, 1, "succeeded", 987
        ),
    )
    return replace(
        summary,
        elapsed_seconds=2.5,
        invocation_count=3,
        invocation_timings=timings,
        node_token_aggregates=(
            NodeTokenAggregate(
                "fanout",
                total=100,
                report_count=2,
                observed_calls=2,
                calls_with_reports=2,
                exact=True,
            ),
        ),
        artifact_references=tuple(
            ArtifactReferenceSummary(
                "fanout",
                f"alpha-{index}",
                None,
                1,
                str(tmp_path / f"résult ({index})#100%.md"),
                None,
            )
            for index in range(2)
        ),
    )


def test_new_summary_normalizes_parallel_order_and_recorded_timing(
    tmp_path: Path,
) -> None:
    left = _parallel_summary(tmp_path)
    right = replace(
        left,
        elapsed_seconds=4.0,
        invocation_timings=(
            replace(left.invocation_timings[0], duration_ms=2000),
            replace(left.invocation_timings[2], duration_ms=1500),
            replace(left.invocation_timings[1], duration_ms=0),
        ),
        artifact_references=tuple(reversed(left.artifact_references)),
    )
    first = render_run_summary_markdown(left)
    second = render_run_summary_markdown(right)
    assert first != second
    normalized = probe.normalized_summary(first, tmp_path, "run")
    assert normalized == probe.normalized_summary(second, tmp_path, "run")
    assert "model café" in normalized
    assert "%C3%A9sult%20%280%29%23100%25.md" in normalized
    assert r"- \`fanout\` / \`alpha-0\`" in normalized


@pytest.mark.parametrize(
    "changed_field",
    ["link", "unicode", "status", "newline", "tokens", "node", "coverage", "reasoning"],
)
def test_new_summary_normalization_keeps_evidence_differences(
    tmp_path: Path, changed_field: str
) -> None:
    summary = _parallel_summary(tmp_path)
    original = render_run_summary_markdown(summary)
    if changed_field == "link":
        changed = render_run_summary_markdown(
            replace(
                summary,
                artifact_references=(
                    replace(
                        summary.artifact_references[0],
                        output_file=str(tmp_path / "different.md"),
                    ),
                    *summary.artifact_references[1:],
                ),
            )
        )
    elif changed_field == "unicode":
        changed = original.replace("model café", "model cafe")
    elif changed_field == "status":
        changed = render_run_summary_markdown(
            replace(
                summary,
                invocation_timings=(
                    replace(summary.invocation_timings[0], status="cancelled"),
                    *summary.invocation_timings[1:],
                ),
            )
        )
    elif changed_field == "reasoning":
        changed = render_run_summary_markdown(
            replace(
                summary,
                invocation_timings=(
                    replace(summary.invocation_timings[0], requested_reasoning="high"),
                    *summary.invocation_timings[1:],
                ),
            )
        )
    elif changed_field in {"tokens", "node", "coverage"}:
        node = summary.node_token_aggregates[0]
        if changed_field == "tokens":
            node = replace(node, total=200)
        elif changed_field == "node":
            node = replace(node, node_id="other-node")
        else:
            node = replace(node, calls_with_reports=1)
        changed = render_run_summary_markdown(
            replace(summary, node_token_aggregates=(node,))
        )
    else:
        changed = original.replace("\n", "\r\n")
    assert probe.normalized_summary(
        original, tmp_path, "run"
    ) != probe.normalized_summary(changed, tmp_path, "run")


def test_new_summary_normalization_preserves_literal_hashes_and_fences(
    tmp_path: Path,
) -> None:
    chart = (
        "## Agent call durations\r\n\r\n"
        "````text\r\n"
        "Call        Model / requested reasoning  Status     Relative runtime                Duration\r\n"
        "fanout / z  model#tag / high             succeeded  ##############################  1m 00.0s\r\n"
        "fanout / a  model#tag / high             failed     ###############...............      30.0s\r\n"
        "````\r\n\r\n"
        "Scale: longest recorded call = 1m 00.0s. 2 shown of 2 observed; 1 succeeded, 1 failed.\r\n\r\n"
        "## Results and reports\r\n\r\n"
        "[report](résult.md#section)\r\n#### literal hashes\r\n"
    )
    normalized = probe.normalized_summary(chart, tmp_path, "probe-run")
    assert normalized == (
        "## Agent call durations\r\n\r\n"
        "````text\r\n"
        "Call  Model / requested reasoning  Status  Relative runtime  Duration\r\n"
        "fanout / a  model#tag / high             failed  <duration-bar> <elapsed>\r\n"
        "fanout / z  model#tag / high             succeeded  <duration-bar> <elapsed>\r\n"
        "````\r\n\r\n"
        "Scale: longest recorded call = <elapsed>. 2 shown of 2 observed; 1 succeeded, 1 failed.\r\n\r\n"
        "## Results and reports\r\n\r\n"
        "[report](résult.md#section)\r\n#### literal hashes\r\n"
    )


@pytest.mark.parametrize("duration_ms", [1, 0, 59950, 3599950, 3723456])
def test_summary_normalization_handles_chart_units_without_erasing_labels(
    tmp_path: Path, duration_ms: int
) -> None:
    summary = _parallel_summary(tmp_path)
    summary = replace(
        summary,
        invocation_timings=(
            replace(
                summary.invocation_timings[0],
                model="duration: 1.000s",
                requested_reasoning="duration: 2.000s",
            ),
            *summary.invocation_timings[1:],
        ),
    )
    changed = replace(
        summary,
        invocation_timings=(
            replace(summary.invocation_timings[0], duration_ms=duration_ms),
            *summary.invocation_timings[1:],
        ),
    )
    original = probe.normalized_summary(
        render_run_summary_markdown(summary), tmp_path, "run"
    )
    normalized = probe.normalized_summary(
        render_run_summary_markdown(changed), tmp_path, "run"
    )
    assert normalized == original
    assert "duration: 2.000s" in normalized
    assert "duration: 1.000s" in normalized


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_summary_normalization_handles_sub_tenth_second_footer(
    tmp_path: Path, newline: str
) -> None:
    summary = _parallel_summary(tmp_path)
    normalized_summaries = []
    for duration_ms in (50, 150):
        changed = replace(
            summary,
            invocation_timings=tuple(
                replace(timing, duration_ms=duration_ms)
                for timing in summary.invocation_timings
            ),
        )
        markdown = render_run_summary_markdown(changed).replace("\n", newline)
        normalized_summaries.append(probe.normalized_summary(markdown, tmp_path, "run"))

    assert normalized_summaries[0] == normalized_summaries[1]
