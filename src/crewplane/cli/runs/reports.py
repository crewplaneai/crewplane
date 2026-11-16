"""Read and present saved reports without rebuilding historical artifacts."""

from __future__ import annotations

import re
from pathlib import Path

import typer
from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown, TableElement
from rich.table import Table

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import (
    contained_directory,
    contained_regular_file,
)
from crewplane.artifacts.naming import run_summary_relative_path
from crewplane.artifacts.run_history import RunHistoryRecord


class _WrappingTable(TableElement):
    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        for rendered in super().__rich_console__(console, options):
            if isinstance(rendered, Table):
                rendered.box = box.ASCII
                rendered.pad_edge = True
                rendered.collapse_padding = False
                rendered.width = max(options.max_width, 5 * len(rendered.columns) + 1)
                for column in rendered.columns:
                    column.overflow = "fold"
                    column.min_width = 2
            yield rendered


class _SavedReportMarkdown(Markdown):
    elements = {**Markdown.elements, "table_open": _WrappingTable}


class SavedReportError(RuntimeError):
    """A saved report or requested desktop target is unavailable."""


def show_saved_report(record: RunHistoryRecord, console: Console) -> None:
    relative = run_summary_relative_path()
    path = record.run_dir / relative
    try:
        report = read_contained_bytes(record.run_dir, relative.as_posix()).decode(
            "utf-8"
        )
        if contained_regular_file(record.run_dir, relative.as_posix()) is None:
            raise ValueError("Summary path changed during protected read.")
    except (OSError, ValueError) as exc:
        raise SavedReportError(_report_error(record, path)) from exc
    _print_context(console, f"Run: {record.manifest.run_key_name}")
    _print_context(console, f"Recorded manifest outcome: {record.manifest.status}")
    _print_context(console, f"Summary: {path}")
    _print_context(console, f"Relative-link base directory: {path.parent}")
    console.print(
        "Saved report snapshot (may predate recovery or finalization):", markup=False
    )
    console.print(
        _SavedReportMarkdown(
            _encodable(console, _visible_details(report)), hyperlinks=False
        ),
        crop=False,
    )


def open_saved_target(record: RunHistoryRecord, results: bool = False) -> Path:
    path = (
        record.results_dir if results else record.run_dir / run_summary_relative_path()
    )
    try:
        if results:
            target = contained_directory(
                record.results_dir.parent, record.results_dir.name
            )
        else:
            target = contained_regular_file(
                record.run_dir, run_summary_relative_path().as_posix()
            )
        if target is None:
            raise SavedReportError(
                f"Requested target is missing or unsafe: {path}. No alternate target was opened."
            )
        target = target.absolute()
        status = typer.launch(str(target))
    except SavedReportError:
        raise
    except Exception as exc:
        raise SavedReportError(_launch_error(record, path)) from exc
    if status != 0:
        raise SavedReportError(_launch_error(record, path))
    return target


def _report_error(record: RunHistoryRecord, path: Path) -> str:
    key = record.manifest.run_key_name
    return (
        f"Cannot safely read saved summary at {path}. It may be missing, unreadable, "
        "invalid UTF-8, or changing; retry if publication is in progress. "
        f"Try 'crewplane runs logs {key}' or 'crewplane runs open {key} --results'."
    )


def _launch_error(record: RunHistoryRecord, path: Path) -> str:
    return (
        f"Cannot submit an open request for {path}. Check the target and desktop opener; "
        f"on a headless system use 'crewplane runs show {record.manifest.run_key_name}'."
    )


def _visible_details(report: str) -> str:
    report = re.sub(r"(?m)^<details>\s*$", "", report)
    report = re.sub(r"(?m)^<summary>([^\n]*)</summary>\s*$", r"### \1\n", report)
    return re.sub(r"(?m)^</details>\s*$", "", report)


def _print_context(console: Console, text: str) -> None:
    console.print(_encodable(console, text), markup=False)


def _encodable(console: Console, text: str) -> str:
    return text.encode(console.encoding, errors="backslashreplace").decode(
        console.encoding
    )
