from __future__ import annotations

import asyncio


class WorkspaceFinalizationDeferredCancellation(asyncio.CancelledError):
    """Cancellation whose workspace finalizer continues under runtime ownership."""


def workspace_finalization_is_deferred(cancellation: BaseException) -> bool:
    return isinstance(cancellation, WorkspaceFinalizationDeferredCancellation)
