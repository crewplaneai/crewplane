from typing import TYPE_CHECKING

# Submodule CLIs run package __init__ first, so keep the compact runtime lazy.
if TYPE_CHECKING:
    from .compact import (
        DEFAULT_QUIET_AFTER_SECONDS,
        DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
        TmuxCompactRuntime,
        build_attach_command,
    )

__all__ = [
    "DEFAULT_QUIET_AFTER_SECONDS",
    "DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS",
    "TmuxCompactRuntime",
    "build_attach_command",
]


def __getattr__(name: str) -> object:
    if name in __all__:
        from .compact import (
            DEFAULT_QUIET_AFTER_SECONDS,
            DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
            TmuxCompactRuntime,
            build_attach_command,
        )

        exports = {
            "DEFAULT_QUIET_AFTER_SECONDS": DEFAULT_QUIET_AFTER_SECONDS,
            "DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS": DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
            "TmuxCompactRuntime": TmuxCompactRuntime,
            "build_attach_command": build_attach_command,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
