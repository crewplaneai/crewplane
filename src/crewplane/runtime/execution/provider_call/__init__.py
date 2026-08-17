from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

from .display import ProviderCallDisplay
from .generated_files import (
    finalize_successful_workspace,
    mark_workspace_succeeded,
    record_generated_file_workspace,
)
from .lifecycle import (
    bind_invocation_output,
    publish_invocation_output,
    read_bound_invocation_output,
    resolve_provider_model,
    run_provider_invocation_lifecycle,
)
from .types import ProviderCallRequest, ProviderCallResult, ProviderOutputPolicy

__all__ = [
    "ProviderCallDisplay",
    "ProviderCallRequest",
    "ProviderCallResult",
    "ProviderOutputPolicy",
    "bind_invocation_output",
    "finalize_successful_workspace",
    "mark_workspace_succeeded",
    "publish_invocation_output",
    "read_bound_invocation_output",
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
    if request.invocation_output_file is None:
        with tempfile.TemporaryDirectory(prefix="crewplane-invocation-") as private_dir:
            return await run_provider_invocation(
                replace(
                    request,
                    invocation_output_file=Path(private_dir) / "provider-output.md",
                ),
                invocation_semaphore=invocation_semaphore,
                capture_exception=capture_exception,
                display=display,
            )
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
