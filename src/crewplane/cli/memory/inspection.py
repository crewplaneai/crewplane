from __future__ import annotations

import hashlib
from pathlib import Path

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.core.memory.models import EvidenceReceipt, MemoryRecord
from crewplane.core.memory.query import tokens


def original_status(project_root: Path, evidence: EvidenceReceipt) -> str:
    path = Path(evidence.source_locator)
    if not path.is_absolute():
        path = project_root / path
    try:
        relative = path.relative_to(project_root).as_posix()
        payload = read_contained_bytes(project_root, relative, 16 * 1024 * 1024)
    except FileNotFoundError:
        return "missing (retained evidence remains available)"
    except (OSError, ValueError):
        return "unavailable or unsafe (retained evidence remains available)"
    return (
        "available and unchanged"
        if hashlib.sha256(payload).hexdigest() == evidence.source_sha256
        else "changed (retained evidence remains unchanged)"
    )


def related_records(
    record: MemoryRecord, records: tuple[MemoryRecord, ...]
) -> tuple[str, ...]:
    query = set(
        tokens(record.revisions[-1].title + " " + " ".join(record.revisions[-1].tags))[
            0
        ]
    )
    return tuple(
        other.record_id
        for other in records
        if other.record_id != record.record_id
        and any(
            query & set(tokens(revision.title + " " + " ".join(revision.tags))[0])
            for revision in other.revisions
        )
    )
