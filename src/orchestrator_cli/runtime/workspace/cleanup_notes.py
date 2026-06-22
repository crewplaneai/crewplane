from __future__ import annotations


def note_cleanup_failure(
    primary: BaseException,
    action: str,
    cleanup_error: BaseException,
) -> None:
    primary.add_note(f"{action} failed: {cleanup_error}")
