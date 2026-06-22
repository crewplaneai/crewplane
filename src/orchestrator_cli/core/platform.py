from __future__ import annotations

import os
import platform


def is_native_windows() -> bool:
    return platform.system() == "Windows"


def supports_posix_process_groups() -> bool:
    return os.name == "posix"
