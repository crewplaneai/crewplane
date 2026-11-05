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
    console.print(terminal_text(value), markup=False, highlight=False)


def revision_content(revision: MemoryRevision) -> list[str]:
    lines = (revision.title + "\n" + revision.body).splitlines()
    if revision.tags:
        lines.append("Tags: " + ", ".join(revision.tags))
    lines.extend(f"Qualification: {item}" for item in revision.qualifications)
    return lines


def revision_card(record: MemoryRecord, revision: MemoryRevision) -> str:
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
    if revision.tags:
        lines.append("Tags: " + ", ".join(revision.tags))
    lines.extend(f"Qualification: {item}" for item in revision.qualifications)
    lines.append(
        f"Authorship: {revision.provenance.author}; {revision.provenance.source_identity}"
    )
    if not revision.evidence:
        lines.append(
            "Evidence strength: human-authored; no host-verified execution evidence."
        )
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
    if revision.expected_accepted_revision is not None:
        previous = next(
            item
            for item in record.revisions
            if item.revision_id == revision.expected_accepted_revision
        )
        lines.append("Correction diff (displayed predecessor / proposed):")
        lines.extend(
            difflib.unified_diff(
                revision_content(previous),
                revision_content(revision),
                fromfile=previous.revision_id,
                tofile=revision.revision_id,
                lineterm="",
            )
        )
    return "\n".join(lines)
