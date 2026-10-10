"""Select protected output opening and staging within provider publication."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from crewplane.core.file_hashing import ContentSignature


@dataclass(frozen=True)
class ProviderOutputOperations:
    open_output: Callable[[Path], AbstractContextManager[tuple[int, os.stat_result]]]
    stage_output: Callable[
        [Path, Path], AbstractContextManager[tuple[Path, ContentSignature]]
    ]


def provider_output_operations() -> ProviderOutputOperations:
    if os.name == "nt":
        from . import provider_output_windows

        return ProviderOutputOperations(
            provider_output_windows.open_output,
            provider_output_windows.stage_output,
        )
    from . import provider_output_posix

    return ProviderOutputOperations(
        provider_output_posix.open_output,
        provider_output_posix.stage_output,
    )
