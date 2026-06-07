from __future__ import annotations

from dataclasses import dataclass

from orchestrator_cli.observability.tmux.client import TmuxSessionClient
from orchestrator_cli.observability.tmux.commands import (
    DASHBOARD_KEY_TABLE,
    INSPECT_KEY_TABLE,
)
from orchestrator_cli.observability.tmux.runtime_files import MODE_INSPECT
from orchestrator_cli.observability.tmux.session import TmuxSessionTargets


@dataclass(frozen=True)
class PaneGeometry:
    left_width: int
    left_height: int
    right_width: int
    right_height: int


class TmuxCompactControlState:
    """Restore tmux control state after pane geometry changes."""

    def __init__(self) -> None:
        self._last_pane_geometry: PaneGeometry | None = None

    def reset(self) -> None:
        self._last_pane_geometry = None

    def recover_after_resize(
        self,
        tmux: TmuxSessionClient,
        mode: str,
        session: TmuxSessionTargets,
        pane_geometry: PaneGeometry,
    ) -> bool:
        previous_geometry = self._last_pane_geometry
        if previous_geometry == pane_geometry:
            return True

        recovered = self.restore_control_state(tmux, mode, session)
        if recovered:
            self._last_pane_geometry = pane_geometry
        return recovered

    def restore_control_state(
        self,
        tmux: TmuxSessionClient,
        mode: str,
        session: TmuxSessionTargets,
    ) -> bool:
        if mode == MODE_INSPECT:
            key_table_result = tmux.run(
                [
                    "set-option",
                    "-t",
                    session.session_name,
                    "key-table",
                    INSPECT_KEY_TABLE,
                ],
                check=False,
            )
            pane_result = tmux.run(
                ["select-pane", "-t", session.right_pane_id],
                check=False,
            )
            return key_table_result.returncode == 0 and pane_result.returncode == 0

        for pane_id in (session.left_pane_id, session.right_pane_id):
            tmux.run(
                ["send-keys", "-X", "-t", pane_id, "cancel"],
                check=False,
            )
        key_table_result = tmux.run(
            [
                "set-option",
                "-t",
                session.session_name,
                "key-table",
                DASHBOARD_KEY_TABLE,
            ],
            check=False,
        )
        pane_result = tmux.run(
            ["select-pane", "-t", session.left_pane_id],
            check=False,
        )
        return key_table_result.returncode == 0 and pane_result.returncode == 0
