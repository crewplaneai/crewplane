from __future__ import annotations

import asyncio

from .provider_display import ProviderCallDisplay
from .provider_invocation_generated_files import (
    finalize_successful_workspace as finalize_successful_workspace,
)
from .provider_invocation_generated_files import (
    mark_workspace_succeeded as mark_workspace_succeeded,
)
from .provider_invocation_generated_files import (
    record_generated_file_workspace as record_generated_file_workspace,
)
from .provider_invocation_lifecycle import (
    resolve_provider_model as resolve_provider_model,
)
from .provider_invocation_lifecycle import (
    run_provider_invocation_lifecycle,
)
from .provider_invocation_types import ProviderCallRequest, ProviderCallResult

__all__ = [
    "ProviderCallDisplay",
    "ProviderCallRequest",
    "ProviderCallResult",
    "finalize_successful_workspace",
    "mark_workspace_succeeded",
    "record_generated_file_workspace",
    "resolve_provider_model",
    "run_provider_call",
    "run_provider_invocation",
]


async def run_provider_invocation(
    request: ProviderCallRequest,
    invocation_semaphore: asyncio.Semaphore | None = None,
    capture_exception: bool = False,
    display: ProviderCallDisplay | None = None,
) -> ProviderCallResult:
    selected_display = display or ProviderCallDisplay(telemetry=request.telemetry)
    if invocation_semaphore is None:
        return await run_provider_invocation_lifecycle(
            request,
            capture_exception,
            selected_display,
        )

    async with invocation_semaphore:
        return await run_provider_invocation_lifecycle(
            request,
            capture_exception,
            selected_display,
        )


async def run_provider_call(
    request: ProviderCallRequest,
    display: ProviderCallDisplay | None = None,
) -> None:
    await run_provider_invocation(request, display=display)
