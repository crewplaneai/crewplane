"""Shared saved-run discovery diagnostics and identifier resolution."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from crewplane.artifacts.run_history import RunHistoryRecord, RunHistoryScan


class RunSelectionError(ValueError):
    """A saved run selector does not identify exactly one valid execution."""


def resolve_run(history: RunHistoryScan, selector: str) -> RunHistoryRecord:
    """Resolve a selector without I/O or mutation, returning the original record.

    Use the selector verbatim. Prefer latest, the first exact full-key match,
    an excluded-key diagnostic, exact run-ID matches, then key/ID prefixes.
    Latest selects the first history record. Ambiguity candidates retain
    history order.

    Raises:
        RunSelectionError: The selector is empty or path-like, latest has no
            records, the key was excluded, or matches are absent or ambiguous.
    """
    _validate_run_selector(selector)
    records = history.records
    if selector == "latest":
        if records:
            return records[0]
        raise RunSelectionError(
            "No saved execution runs found. Use crewplane runs list."
        )
    exact_key = [row for row in records if row.manifest.run_key_name == selector]
    if exact_key:
        return exact_key[0]
    for diagnostic in history.diagnostics:
        if diagnostic.run_key_name == selector:
            raise RunSelectionError(
                f"Saved run {selector!r} was excluded ({diagnostic.category}): "
                f"{diagnostic.message} Path: {diagnostic.path}"
            )
    exact_id = [row for row in records if row.manifest.run_id == selector]
    matches = exact_id or [
        row
        for row in records
        if row.manifest.run_id.startswith(selector)
        or row.manifest.run_key_name.startswith(selector)
    ]
    return _select_unique_run(matches, selector)


def _validate_run_selector(selector: str) -> None:
    """Reject empty or path-like selectors without normalizing their text."""
    if (
        not selector.strip()
        or selector == "."
        or any(part in selector for part in ("/", "\\", ".."))
    ):
        raise RunSelectionError(
            "RUN must be a nonempty identifier, not a path. Use crewplane runs list."
        )


def _select_unique_run(
    matches: list[RunHistoryRecord], selector: str
) -> RunHistoryRecord:
    """Return the sole match or report missing/ambiguous matches in input order."""
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RunSelectionError(
            f"No saved execution run matches {selector!r}. Use crewplane runs list."
        )
    candidates = "\n".join(
        f"  {row.manifest.workflow_name} | {row.manifest.started_at} | "
        f"{row.manifest.run_key_name}"
        for row in matches
    )
    raise RunSelectionError(
        f"Ambiguous saved run {selector!r}; use an exact full key:\n{candidates}"
    )


def run_selectors(history: RunHistoryScan) -> dict[str, str]:
    """Map full keys to display selectors in history order without I/O or mutation.

    Use a run ID when unique across the records; use the full key for every
    record whose run ID is duplicated.
    """
    counts = Counter(record.manifest.run_id for record in history.records)
    return {
        record.manifest.run_key_name: (
            record.manifest.run_id
            if counts[record.manifest.run_id] == 1
            else record.manifest.run_key_name
        )
        for record in history.records
    }


def warn_excluded_runs(history: RunHistoryScan) -> None:
    """Write exclusion warnings to stderr without changing history or stdout.

    Aggregate all no_manifest diagnostics into one warning first, then emit
    other diagnostics individually in their original order. Empty diagnostics
    produce no output.
    """
    missing = [item for item in history.diagnostics if item.category == "no_manifest"]
    if missing:
        typer.echo(
            f"Excluded {len(missing)} directory(s) without execution manifests; "
            "these may contain preflight or incomplete attempts.",
            err=True,
        )
    for diagnostic in history.diagnostics:
        if diagnostic.category != "no_manifest":
            typer.echo(
                f"Excluded {diagnostic.run_key_name!r} ({diagnostic.category}): "
                f"{diagnostic.message} Path: {diagnostic.path}",
                err=True,
            )
