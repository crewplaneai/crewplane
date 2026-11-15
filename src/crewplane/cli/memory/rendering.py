from __future__ import annotations

import difflib
import unicodedata

from rich.console import Console

from crewplane.core.memory.models import MemoryRecord, MemoryRevision


def terminal_text(value: str) -> str:
    """Keep line structure, but make terminal control and bidi bytes visible."""
    return "".join(
        character
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
        else f"\\u{ord(character):04x}"
        for character in value
    )


def print_literal(console: Console, value: str) -> None:
    """Escape terminal controls and bidi characters, then print without styling."""
    console.print(terminal_text(value), markup=False, highlight=False)


def revision_content(revision: MemoryRevision) -> list[str]:
    """Return unsanitized title/body lines, tags, and qualifications for comparison.

    Tags and qualifications retain their order; empty tags are omitted.
    Evidence, provenance, and revision bookkeeping are excluded.
    """
    lines = (revision.title + "\n" + revision.body).splitlines()
    lines.extend(_revision_metadata(revision))
    return lines


def revision_card(record: MemoryRecord, revision: MemoryRevision) -> str:
    """Build a complete revision card for display through ``print_literal``.

    The result is unsanitized. For a correction, record.revisions must contain
    the predecessor identified by revision.expected_accepted_revision.

    Raises:
        StopIteration: The supplied record does not contain the predecessor.
    """
    lines = [
        f"[ ] {revision.title}",
        f"Record: {record.record_id}",
        f"Revision: {revision.revision_id} ({revision.disposition})",
        f"Candidate digest: {revision.candidate_digest}",
        f"Expected accepted revision: {revision.expected_accepted_revision or 'none'}",
        f"Current accepted revision: {record.accepted_revision or 'none'}",
        "",
        revision.body,
    ]
    lines.extend(_revision_metadata(revision))
    lines.append(
        f"Authorship: {revision.provenance.author}; {revision.provenance.source_identity}"
    )
    lines.extend(_revision_evidence(revision))
    lines.extend(_correction_diff(record, revision))
    return "\n".join(lines)


def _revision_metadata(revision: MemoryRevision) -> list[str]:
    lines = ["Tags: " + ", ".join(revision.tags)] if revision.tags else []
    lines.extend(f"Qualification: {item}" for item in revision.qualifications)
    return lines


def _revision_evidence(revision: MemoryRevision) -> list[str]:
    if not revision.evidence:
        return [
            "Evidence strength: human-authored; no host-verified execution evidence."
        ]
    lines: list[str] = []
    for evidence in revision.evidence:
        lines.extend(
            (
                f"Evidence {evidence.evidence_id}: {evidence.kind}",
                f"Source: {evidence.source_locator}",
                f"Source SHA256: {evidence.source_sha256}",
                f"Observed: {evidence.observed_at}; retained: {evidence.captured_at}",
                "Retained excerpt:",
                evidence.excerpt,
            )
        )
        lines.extend(f"Outcome limitation: {item}" for item in evidence.qualifications)
        lines.extend(f"Provenance: {item}" for item in evidence.provenance)
    return lines


def _correction_diff(record: MemoryRecord, revision: MemoryRevision) -> list[str]:
    if revision.expected_accepted_revision is None:
        return []
    previous = next(
        item
        for item in record.revisions
        if item.revision_id == revision.expected_accepted_revision
    )
    return [
        "Correction diff (displayed predecessor / proposed):",
        *difflib.unified_diff(
            revision_content(previous),
            revision_content(revision),
            fromfile=previous.revision_id,
            tofile=revision.revision_id,
            lineterm="",
        ),
    ]
