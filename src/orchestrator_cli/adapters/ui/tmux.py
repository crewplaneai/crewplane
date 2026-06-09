from __future__ import annotations

import math
import shutil
from collections.abc import Callable

from rich.console import Console

from orchestrator_cli.architecture.contracts import (
    CanonicalIntegrationConfig,
    JsonObject,
    TmuxUiOptions,
)
from orchestrator_cli.architecture.ports import UIAdapterCapabilities
from orchestrator_cli.architecture.ports.runtime import UIRuntimePlan
from orchestrator_cli.core.config import Config
from orchestrator_cli.observability.tmux.compact import TmuxCompactRuntime
from orchestrator_cli.observability.types import WorkflowTopology
from orchestrator_cli.versions import INTEGRATION_API_VERSION


def _resolve_tmux_options(options: JsonObject) -> TmuxUiOptions:
    resolved = dict(options)

    auto_close_session_raw = resolved.pop("auto_close_session", True)
    if not isinstance(auto_close_session_raw, bool):
        raise ValueError("tmux ui option 'auto_close_session' must be a boolean")

    tmux_executable_raw = resolved.pop("tmux_executable", "tmux")
    if not isinstance(tmux_executable_raw, str) or not tmux_executable_raw.strip():
        raise ValueError("tmux ui option 'tmux_executable' must be a non-empty string")

    quiet_after_seconds_raw = resolved.pop("quiet_after_seconds", 120.0)
    if isinstance(quiet_after_seconds_raw, bool) or not isinstance(
        quiet_after_seconds_raw,
        (int, float),
    ):
        raise ValueError("tmux ui option 'quiet_after_seconds' must be a number >= 1.0")
    quiet_after_seconds = float(quiet_after_seconds_raw)
    if not math.isfinite(quiet_after_seconds) or quiet_after_seconds < 1.0:
        raise ValueError("tmux ui option 'quiet_after_seconds' must be a number >= 1.0")

    log_tail_lines_raw = resolved.pop("log_tail_lines", None)
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

    if resolved:
        raise ValueError(f"Unsupported tmux ui options: {', '.join(sorted(resolved))}")

    return TmuxUiOptions(
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
        options: JsonObject | None = None,
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
            api_version=INTEGRATION_API_VERSION,
            options=canonical_options,
            option_scopes={key: "observer" for key in canonical_options},
            capabilities={
                "accepts_which_override": self.capabilities.accepts_which_override,
                "requires_cli_output_logs": self.capabilities.requires_cli_output_logs,
            },
        )

    def create_runtime(
        self,
        config: Config,
        workflow_topology: WorkflowTopology,
        run_id: str,
        console: Console,
        options: JsonObject | None = None,
        warning_sink: Callable[[str], None] | None = None,
        which_fn: Callable[[str], str | None] | None = None,
    ) -> UIRuntimePlan:
        """Return a tmux observer plan or degrade cleanly when tmux is unavailable."""

        _validate_runtime_request(config, workflow_topology, run_id, console)
        runtime_options = _resolve_tmux_options(dict(options or {}))
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


def _validate_runtime_request(
    config: Config,
    workflow_topology: WorkflowTopology,
    run_id: str,
    console: Console,
) -> None:
    if not isinstance(config, Config):
        raise TypeError("config must be a Config instance")
    if not isinstance(workflow_topology, WorkflowTopology):
        raise TypeError("workflow_topology must be a WorkflowTopology instance")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if not isinstance(console, Console):
        raise TypeError("console must be a Console instance")
