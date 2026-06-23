from __future__ import annotations

from crewplane.observability.tmux.client import TmuxSessionClient
from crewplane.observability.tmux.commands import (
    DASHBOARD_KEY_TABLE,
    PANE_TITLE_OPTION,
)
from crewplane.observability.tmux.session import TmuxSessionTargets


class TmuxCompactWindowOptions:
    """Own compact dashboard tmux window options and option caches."""

    def __init__(self) -> None:
        self._status_left_cache: str | None = None
        self._status_right_cache: str | None = None
        self._pane_title_cache: dict[str, str] = {}

    def reset(self) -> None:
        self._status_left_cache = None
        self._status_right_cache = None
        self._pane_title_cache = {}

    def configure(
        self,
        tmux: TmuxSessionClient,
        session: TmuxSessionTargets,
    ) -> None:
        tmux.run(
            ["set-window-option", "-t", session.window_target, "remain-on-exit", "on"]
        )
        tmux.run(
            [
                "set-window-option",
                "-t",
                session.window_target,
                "pane-border-status",
                "top",
            ]
        )
        tmux.run(
            [
                "set-window-option",
                "-t",
                session.window_target,
                "pane-border-format",
                f"#{{{PANE_TITLE_OPTION}}}",
            ]
        )
        tmux.run(
            ["set-option", "-t", session.session_name, "status-left-length", "200"]
        )
        tmux.run(
            ["set-option", "-t", session.session_name, "status-right-length", "120"]
        )
        tmux.run(["set-option", "-t", session.session_name, "mouse", "on"])
        tmux.run(
            [
                "set-option",
                "-t",
                session.session_name,
                "key-table",
                DASHBOARD_KEY_TABLE,
            ]
        )
        tmux.run(
            [
                "set-option",
                "-t",
                session.session_name,
                "pane-border-style",
                "fg=colour238",
            ],
            check=False,
        )
        tmux.run(
            [
                "set-option",
                "-t",
                session.session_name,
                "pane-active-border-style",
                "fg=colour46",
            ],
            check=False,
        )
        self.set_pane_title_if_changed(tmux, session.left_pane_id, "DAG Summary")
        self.set_pane_title_if_changed(tmux, session.right_pane_id, "Node Output")

    def set_status_option_if_changed(
        self,
        tmux: TmuxSessionClient,
        session_name: str,
        option: str,
        value: str,
    ) -> None:
        if self._status_cache_matches(option, value):
            return
        result = tmux.run(
            ["set-option", "-t", session_name, option, value],
            check=False,
        )
        if result.returncode == 0:
            self._update_status_cache(option, value)

    def set_pane_title_if_changed(
        self,
        tmux: TmuxSessionClient,
        pane_id: str,
        title: str,
    ) -> None:
        if self._pane_title_cache.get(pane_id) == title:
            return
        result = tmux.run(
            ["set-option", "-p", "-t", pane_id, PANE_TITLE_OPTION, title],
            check=False,
        )
        if result.returncode == 0:
            self._pane_title_cache[pane_id] = title

    def _status_cache_matches(self, option: str, value: str) -> bool:
        if option == "status-left":
            return self._status_left_cache == value
        if option == "status-right":
            return self._status_right_cache == value
        raise ValueError(f"Unsupported tmux status option '{option}'.")

    def _update_status_cache(self, option: str, value: str) -> None:
        if option == "status-left":
            self._status_left_cache = value
            return
        if option == "status-right":
            self._status_right_cache = value
            return
        raise ValueError(f"Unsupported tmux status option '{option}'.")
