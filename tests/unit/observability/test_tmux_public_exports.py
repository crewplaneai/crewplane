from inspect import signature

import crewplane.observability as observability_exports
import crewplane.observability.tmux as tmux_exports
from crewplane.architecture.contracts import TmuxUiOptions
from crewplane.observability.tmux.compact import (
    DEFAULT_QUIET_AFTER_SECONDS,
    DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    TmuxCompactRuntime,
    build_attach_command,
)
from crewplane.observability.tmux.refresh import TmuxCompactRefreshController


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


def test_direct_tmux_constructor_defaults_match_options_contract() -> None:
    defaults = TmuxUiOptions()
    assert DEFAULT_QUIET_AFTER_SECONDS == defaults.quiet_after_seconds == 120.0
    runtime = signature(TmuxCompactRuntime).parameters
    for field in (
        "auto_close_session",
        "tmux_executable",
        "quiet_after_seconds",
        "log_tail_lines",
    ):
        assert runtime[field].default == getattr(defaults, field)
    refresh = signature(TmuxCompactRefreshController).parameters
    for field in ("quiet_after_seconds", "log_tail_lines"):
        assert refresh[field].default == getattr(defaults, field)
