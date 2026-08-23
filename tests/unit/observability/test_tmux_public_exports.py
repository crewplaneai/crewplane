import crewplane.observability as observability_exports
import crewplane.observability.tmux as tmux_exports
from crewplane.observability.tmux.compact import (
    DEFAULT_QUIET_AFTER_SECONDS,
    DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    TmuxCompactRuntime,
    build_attach_command,
)


def test_public_tmux_exports_resolve_from_both_packages() -> None:
    assert observability_exports.TmuxCompactRuntime is TmuxCompactRuntime
    assert observability_exports.build_attach_command is build_attach_command
    assert tmux_exports.DEFAULT_QUIET_AFTER_SECONDS == DEFAULT_QUIET_AFTER_SECONDS
    assert (
        tmux_exports.DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS
        == DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS
    )
    assert tmux_exports.TmuxCompactRuntime is TmuxCompactRuntime
    assert tmux_exports.build_attach_command is build_attach_command
