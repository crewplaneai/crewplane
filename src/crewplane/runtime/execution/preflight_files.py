from pathlib import Path

from crewplane.architecture.safe_files import contained_regular_file


def preflight_content_path(
    context_root: str, content_ref: str, content_kind: str
) -> Path | None:
    """Look up a compiled bundle file without choosing a read or missing-file policy."""
    normalized_ref = Path(content_ref)
    if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
        raise ValueError(f"Invalid {content_kind} content reference '{content_ref}'.")
    return contained_regular_file(
        Path(context_root) / "preflight", normalized_ref.as_posix()
    )
