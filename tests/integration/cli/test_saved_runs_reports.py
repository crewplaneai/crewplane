from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.cli.runs import reports
from tests.helpers.platforms import symlink_or_skip
from tests.helpers.resume import make_run_manifest, write_run_manifest


@pytest.fixture
def record(tmp_path: Path) -> RunHistoryRecord:
    state = tmp_path / ".crewplane"
    manifest = make_run_manifest("run-1", "workflow-run-1", "failed")
    metadata = write_run_manifest(state, manifest)
    return RunHistoryRecord(
        manifest,
        metadata,
        metadata.parent.parent,
        state / "execution-results" / manifest.run_key_name,
    )


def write_summary(record: RunHistoryRecord, content: bytes) -> Path:
    path = record.run_dir / "logs" / "summary.md"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("width", [30, 80, 120])
def test_saved_snapshot_and_details_survive_rendering(
    record: RunHistoryRecord, width: int
) -> None:
    labels = [
        "Invocation usage and timing details",
        "Workspace Observability",
        "All retained issues",
        "Complete stage table",
        "Artifact References",
    ]
    body = "# Saved succeeded sentinel\n\n" + "\n\n".join(
        f"<details>\n<summary>{label}</summary>\n\nbody-{index}\n\n</details>"
        for index, label in enumerate(labels)
    )
    path = write_summary(record, body.encode())
    original = path.read_bytes()
    output = io.StringIO()
    reports.show_saved_report(
        record, Console(file=output, width=width, force_terminal=False)
    )
    rendered = " ".join(output.getvalue().split())
    assert "Recorded manifest outcome: failed" in rendered
    assert "Saved succeeded sentinel" in rendered
    assert "may predate recovery or finalization" in rendered
    for index, label in enumerate(labels):
        assert label in rendered
        assert f"body-{index}" in rendered
    assert path.read_bytes() == original


def test_relative_links_display_paths_without_terminal_hyperlinks(
    record: RunHistoryRecord,
) -> None:
    path = write_summary(
        record, b"[Saved result](../../execution-results/example/result.md)\n"
    )
    output = io.StringIO()
    reports.show_saved_report(
        record, Console(file=output, width=200, force_terminal=True)
    )
    assert str(path.parent) in output.getvalue()
    assert "../../execution-results/example/result.md" in output.getvalue()
    assert "\x1b]8;" not in output.getvalue()


def test_unicode_report_on_ascii_stream(record: RunHistoryRecord) -> None:
    write_summary(record, "# Résumé 世界\n\nSaved body\n".encode())
    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding="ascii")
    reports.show_saved_report(
        record, Console(file=output, width=40, force_terminal=False)
    )
    output.flush()
    assert b"Saved body" in raw.getvalue()


@pytest.mark.parametrize(
    "kind", ["missing", "utf8", "symlink", "hardlink", "directory", "parent-link"]
)
def test_unavailable_summary_has_actionable_error(
    record: RunHistoryRecord, tmp_path: Path, kind: str
) -> None:
    path = write_summary(record, b"saved report")
    if kind == "missing":
        path.unlink()
    elif kind == "utf8":
        path.write_bytes(b"\xff")
    elif kind == "symlink":
        path.rename(tmp_path / "target")
        symlink_or_skip(path, tmp_path / "target")
    elif kind == "hardlink":
        os.link(path, tmp_path / "alias")
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    else:
        path.parent.rename(record.run_dir / "moved")
        symlink_or_skip(path.parent, record.run_dir / "moved", target_is_directory=True)
    with pytest.raises(reports.SavedReportError) as caught:
        reports.show_saved_report(record, Console(file=io.StringIO()))
    assert str(path) in str(caught.value)
    assert "runs logs workflow-run-1" in str(caught.value)
    assert "runs open workflow-run-1 --results" in str(caught.value)


@pytest.mark.parametrize(
    "failure", [PermissionError("denied"), ValueError("changed during protected read")]
)
def test_protected_read_failures_are_reported(
    record: RunHistoryRecord, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    write_summary(record, b"saved report")

    def fail(root: Path, relative: str) -> bytes:
        assert root == record.run_dir and relative == "logs/summary.md"
        raise failure

    monkeypatch.setattr(reports, "read_contained_bytes", fail)
    with pytest.raises(reports.SavedReportError, match="retry"):
        reports.show_saved_report(record, Console(file=io.StringIO()))


@pytest.mark.parametrize("results", [False, True])
def test_open_launches_only_exact_requested_absolute_path(
    record: RunHistoryRecord, monkeypatch: pytest.MonkeyPatch, results: bool
) -> None:
    summary = write_summary(record, b"saved")
    record.results_dir.mkdir(parents=True)
    calls: list[str] = []

    def launch(path: str) -> int:
        calls.append(path)
        return 0

    monkeypatch.setattr(reports.typer, "launch", launch)
    expected = record.results_dir if results else summary
    assert reports.open_saved_target(record, results) == expected.absolute()
    assert calls == [str(expected.absolute())]


@pytest.mark.parametrize("results", [False, True])
def test_missing_open_target_never_falls_back(
    record: RunHistoryRecord, monkeypatch: pytest.MonkeyPatch, results: bool
) -> None:
    if results:
        write_summary(record, b"saved")
    else:
        record.results_dir.mkdir(parents=True)

    def launch(path: str) -> int:
        assert Path(path).is_absolute()
        pytest.fail("No desktop launch is permitted for a missing target")

    monkeypatch.setattr(reports.typer, "launch", launch)
    with pytest.raises(reports.SavedReportError, match="missing"):
        reports.open_saved_target(record, results)
    assert not (
        record.results_dir if results else record.run_dir / "logs" / "summary.md"
    ).exists()


@pytest.mark.parametrize("failure", [1, OSError("no desktop")])
def test_launcher_failure_recommends_headless_view(
    record: RunHistoryRecord, monkeypatch: pytest.MonkeyPatch, failure: int | Exception
) -> None:
    write_summary(record, b"saved")

    def launch(path: str) -> int:
        assert Path(path).is_absolute()
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(reports.typer, "launch", launch)
    with pytest.raises(
        reports.SavedReportError, match="crewplane runs show workflow-run-1"
    ):
        reports.open_saved_target(record)


@pytest.mark.parametrize("replace_parent", [False, True])
def test_summary_replacement_during_read_fails(
    record: RunHistoryRecord, monkeypatch: pytest.MonkeyPatch, replace_parent: bool
) -> None:
    from crewplane.architecture import safe_file_reads

    path = write_summary(record, b"original report")
    replacement = path.with_name("next-summary.md")
    replacement.write_bytes(b"replacement report")
    original_read = os.read
    replaced = False

    def replace_after_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if not replaced:
            if replace_parent:
                moved = record.run_dir / "moved-logs"
                path.parent.rename(moved)
                symlink_or_skip(path.parent, moved, target_is_directory=True)
            else:
                replacement.replace(path)
            replaced = True
        return chunk

    monkeypatch.setattr(safe_file_reads.os, "read", replace_after_read)
    output = io.StringIO()
    with pytest.raises(reports.SavedReportError, match="retry"):
        reports.show_saved_report(record, Console(file=output))
    assert output.getvalue() == ""


@pytest.mark.parametrize("results", [False, True])
def test_open_rejects_symlink_targets(
    record: RunHistoryRecord, monkeypatch: pytest.MonkeyPatch, results: bool
) -> None:
    summary = write_summary(record, b"saved")
    record.results_dir.mkdir(parents=True)
    target = record.results_dir if results else summary
    moved = target.with_name("moved")
    target.rename(moved)
    symlink_or_skip(target, moved, target_is_directory=results)

    def launch(path: str) -> int:
        pytest.fail(f"Unsafe target must not be launched: {path}")

    monkeypatch.setattr(reports.typer, "launch", launch)
    with pytest.raises(reports.SavedReportError):
        reports.open_saved_target(record, results)


@pytest.mark.parametrize("width", [20, 80, 120])
@pytest.mark.parametrize("encoding", ["utf-8", "ascii"])
def test_saved_markdown_tables_retain_all_cell_content(
    record: RunHistoryRecord, width: int, encoding: str
) -> None:
    headings = [f"Column{index}" for index in range(12)]
    values = [f"complete-value-{index}" for index in range(11)] + [
        "complete-漢字-value-11"
    ]
    markdown = "\n".join(
        "| " + " | ".join(row) + " |" for row in (headings, ["---"] * 12, values)
    )
    path = write_summary(record, markdown.encode("utf-8"))
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding=encoding)
    reports.show_saved_report(
        record, Console(file=stream, width=width, force_terminal=False)
    )
    stream.flush()
    output = raw.getvalue().decode(encoding)
    assert "…" not in output and "..." not in output
    rows: list[list[str]] = []
    fragments: list[list[str]] = [[] for _ in headings]
    for line in output.splitlines():
        cells = line.split("|")
        if len(cells) == len(headings) + 2:
            for column, cell in zip(fragments, cells[1:-1], strict=True):
                column.append(cell.strip())
        elif any(fragments):
            rows.append(["".join(column) for column in fragments])
            fragments = [[] for _ in headings]
    expected_values = [
        value.encode(encoding, errors="backslashreplace").decode(encoding)
        for value in values
    ]
    assert rows == [headings, expected_values]
    assert path.read_bytes() == markdown.encode("utf-8")
