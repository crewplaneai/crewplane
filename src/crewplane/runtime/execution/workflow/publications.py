"""Register workflow artifacts with their current bytes as recovery evidence."""

from pathlib import Path

from crewplane.core.file_hashing import file_size_and_sha256

from ..publication_registry import RuntimePublicationRegistry


def register_recoverable_publication(
    publications: RuntimePublicationRegistry,
    path: Path,
) -> None:
    publications.publish(path, file_size_and_sha256(path), recovery_source=path)
