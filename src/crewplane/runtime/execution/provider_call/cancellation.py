from __future__ import annotations

import asyncio

_WORKSPACE_FINALIZATION_DEFERRED_ATTRIBUTE = (
    "_crewplane_workspace_finalization_deferred"
)


def mark_workspace_finalization_deferred(
    cancellation: asyncio.CancelledError,
) -> None:
    setattr(cancellation, _WORKSPACE_FINALIZATION_DEFERRED_ATTRIBUTE, True)


def workspace_finalization_is_deferred(cancellation: BaseException) -> bool:
    return bool(
        getattr(cancellation, _WORKSPACE_FINALIZATION_DEFERRED_ATTRIBUTE, False)
    )
