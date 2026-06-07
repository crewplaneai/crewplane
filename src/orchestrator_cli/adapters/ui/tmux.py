from __future__ import annotations

import math
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from orchestrator_cli.architecture.api_version import EXT_API_VERSION
from orchestrator_cli.architecture.ports import UIAdapterCapabilities
from orchestrator_cli.architecture.ports.runtime import UIRuntimePlan
from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight.runtime_config import CanonicalIntegrationConfig
from orchestrator_cli.observability.tmux.compact import (
    DEFAULT_QUIET_AFTER_SECONDS,
    TmuxCompactRuntime,
)
from orchestrator_cli.observability.types import WorkflowTopology


@dataclass(frozen=True)
class _ResolvedTmuxOptions:
    auto_close_session: bool
    tmux_executable: str
    quiet_after_seconds: float
    log_tail_lines: int | None


def _resolve_tmux_options(options: dict[str, Any]) -> _ResolvedTmuxOptions:
    auto_close_session_raw = options.pop("auto_close_session", True)
    if not isinstance(auto_close_session_raw, bool):
        raise ValueError("tmux ui option 'auto_close_session' must be a boolean")

    tmux_executable_raw = options.pop("tmux_executable", "tmux")
    if not isinstance(tmux_executable_raw, str) or not tmux_executable_raw.strip():
        raise ValueError("tmux ui option 'tmux_executable' must be a non-empty string")

    quiet_after_seconds_raw = options.pop(
        "quiet_after_seconds",
        DEFAULT_QUIET_AFTER_SECONDS,
    )
    if isinstance(quiet_after_seconds_raw, bool) or not isinstance(
        quiet_after_seconds_raw,
        (int, float),
    ):
        raise ValueError("tmux ui option 'quiet_after_seconds' must be a number >= 1.0")
    quiet_after_seconds = float(quiet_after_seconds_raw)
    if not math.isfinite(quiet_after_seconds) or quiet_after_seconds < 1.0:
        raise ValueError("tmux ui option 'quiet_after_seconds' must be a number >= 1.0")

    log_tail_lines_raw = options.pop("log_tail_lines", None)
    if log_tail_lines_raw is not None:
        if isinstance(log_tail_lines_raw, bool) or not isinstance(
            log_tail_lines_raw,
            int,
        ):
            raise ValueError(
                "tmux ui option 'log_tail_lines' must be null or an integer "
                "between 1 and 200"
            )
        if not 1 <= log_tail_lines_raw <= 200:
            raise ValueError(
                "tmux ui option 'log_tail_lines' must be null or an integer "
                "between 1 and 200"
            )

    if options:
        raise ValueError(f"Unsupported tmux ui options: {', '.join(sorted(options))}")

    return _ResolvedTmuxOptions(
        auto_close_session=auto_close_session_raw,
        tmux_executable=tmux_executable_raw,
        quiet_after_seconds=quiet_after_seconds,
        log_tail_lines=log_tail_lines_raw,
    )


class TmuxUIAdapter:
    """Create the tmux-backed live runtime when tmux is available."""

    capabilities = UIAdapterCapabilities(
        requires_cli_output_logs=True,
        accepts_which_override=True,
    )

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        runtime_options = _resolve_tmux_options(dict(options or {}))
        canonical_options = {
            "auto_close_session": runtime_options.auto_close_session,
            "log_tail_lines": runtime_options.log_tail_lines,
            "quiet_after_seconds": runtime_options.quiet_after_seconds,
            "tmux_executable": runtime_options.tmux_executable,
        }
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            api_version=EXT_API_VERSION,
            options=canonical_options,
            option_scopes={key: "observer" for key in canonical_options},
            capabilities={
                "accepts_which_override": self.capabilities.accepts_which_override,
                "requires_cli_output_logs": self.capabilities.requires_cli_output_logs,
            },
        )

    def create_runtime(
        self,
        config: Config,  # noqa: ARG002 - Required by callback or protocol signature.
        workflow_topology: WorkflowTopology,  # noqa: ARG002 - Required by callback or protocol signature.
        run_id: str,  # noqa: ARG002 - Required by callback or protocol signature.
        console: Console,
        options: Mapping[str, Any] | None = None,
        warning_sink: Callable[[str], None] | None = None,
        which_fn: Callable[[str], str | None] | None = None,
    ) -> UIRuntimePlan:
        """Return a tmux observer plan or degrade cleanly when tmux is unavailable."""

        resolved_options = dict(options or {})
        runtime_options = _resolve_tmux_options(resolved_options)
        which_lookup = shutil.which if which_fn is None else which_fn

        if which_lookup(runtime_options.tmux_executable) is None:
            message = (
                f"{runtime_options.tmux_executable} not found; "
                "continuing without live dashboard."
            )
            if warning_sink is not None:
                warning_sink(message)
            else:
                console.print(f"[yellow]WARN[/] {message}")
            return UIRuntimePlan(
                observers=(),
                suppress_progress_output=False,
            )

        runtime = TmuxCompactRuntime(
            auto_close_session=runtime_options.auto_close_session,
            tmux_executable=runtime_options.tmux_executable,
            warning_sink=warning_sink,
            quiet_after_seconds=runtime_options.quiet_after_seconds,
            log_tail_lines=runtime_options.log_tail_lines,
        )

        return UIRuntimePlan(
            observers=(runtime,),
            suppress_progress_output=True,
        )
