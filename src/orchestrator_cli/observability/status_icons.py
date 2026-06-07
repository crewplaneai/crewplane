from __future__ import annotations

STATUS_ICON_BY_STATE: dict[str, str] = {
    "pending": "⏸",
    "running": "⏳",
    "succeeded": "✅",
    "failed": "❌",
    "blocked": "⛔",
}


def status_icon(status: str) -> str:
    return STATUS_ICON_BY_STATE.get(status, "?")
