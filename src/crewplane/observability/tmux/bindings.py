from __future__ import annotations

from crewplane.observability.tmux.client import TmuxSessionClient
from crewplane.observability.tmux.commands import (
    COPY_MODE_KEY_TABLES,
    DASHBOARD_KEY_TABLE,
    INSPECT_KEY_TABLE,
    LIVE_MOUSE_KEYS,
    ROOT_KEY_TABLE,
    InspectCommandContext,
    copy_mode_binding_commands,
    dashboard_key_bindings,
    focus_commands,
    inspect_copy_mode_key_bindings,
    inspect_enter_command,
    inspect_exit_command,
    inspect_formatted_command,
    inspect_key_bindings,
    inspect_raw_command,
    quit_dashboard_commands,
    selection_move_command,
    tmux_command_string,
)
from crewplane.observability.tmux.runtime_files import (
    MODE_DASHBOARD,
    MODE_INSPECT,
    RuntimeFiles,
)
from crewplane.observability.tmux.session import TmuxSessionTargets


class TmuxCompactKeyBindings:
    """Install and synchronize compact dashboard tmux key bindings."""

    def __init__(
        self,
        tmux_executable: str = "tmux",
        refresh_interval_seconds: float = 0.25,
    ) -> None:
        self._tmux_executable = tmux_executable
        self._refresh_interval_seconds = refresh_interval_seconds
        self._copy_mode_bindings_mode: str | None = None
        self._move_up_command: str | None = None
        self._move_down_command: str | None = None
        self._enter_inspect_command: str | None = None
        self._exit_inspect_command: str | None = None
        self._raw_inspect_command: str | None = None
        self._formatted_inspect_command: str | None = None

    def reset(self) -> None:
        self._copy_mode_bindings_mode = None
        self._move_up_command = None
        self._move_down_command = None
        self._enter_inspect_command = None
        self._exit_inspect_command = None
        self._raw_inspect_command = None
        self._formatted_inspect_command = None

    def install(
        self,
        tmux: TmuxSessionClient,
        runtime_files: RuntimeFiles,
        session: TmuxSessionTargets,
    ) -> None:
        move_up = selection_move_command(
            runtime_files=runtime_files,
            direction="up",
        )
        move_down = selection_move_command(
            runtime_files=runtime_files,
            direction="down",
        )
        inspect_context = InspectCommandContext(
            tmux_executable=self._tmux_executable,
            runtime_files=runtime_files,
            session=session,
        )
        exit_inspect = inspect_exit_command(
            inspect_context,
            refresh_interval_seconds=self._refresh_interval_seconds,
        )
        enter_inspect = inspect_enter_command(
            inspect_context,
        )
        raw_inspect = inspect_raw_command(inspect_context)
        formatted_inspect = inspect_formatted_command(inspect_context)
        self._move_up_command = move_up
        self._move_down_command = move_down
        self._enter_inspect_command = enter_inspect
        self._exit_inspect_command = exit_inspect
        self._raw_inspect_command = raw_inspect
        self._formatted_inspect_command = formatted_inspect

        self._install_dashboard_bindings(
            tmux,
            runtime_files,
            session,
            move_up,
            move_down,
            enter_inspect,
            raw_inspect,
        )
        self._apply_dashboard_copy_mode_bindings(tmux, runtime_files, session)
        self._copy_mode_bindings_mode = MODE_DASHBOARD
        self._install_inspect_bindings(
            tmux,
            runtime_files,
            session,
            exit_inspect,
            raw_inspect,
            formatted_inspect,
        )

    def sync_copy_mode_bindings(
        self,
        tmux: TmuxSessionClient,
        runtime_files: RuntimeFiles,
        session: TmuxSessionTargets,
        mode: str,
    ) -> None:
        if mode == self._copy_mode_bindings_mode:
            return
        if mode == MODE_INSPECT:
            self._apply_inspect_copy_mode_bindings(tmux, runtime_files, session)
        else:
            self._apply_dashboard_copy_mode_bindings(tmux, runtime_files, session)
        self._copy_mode_bindings_mode = mode

    def _install_dashboard_bindings(
        self,
        tmux: TmuxSessionClient,
        runtime_files: RuntimeFiles,
        session: TmuxSessionTargets,
        move_up: str,
        move_down: str,
        enter_inspect: str,
        raw_inspect: str,
    ) -> None:
        dashboard_bindings = dashboard_key_bindings(
            session_name=session.session_name,
            left_pane_id=session.left_pane_id,
            move_up=move_up,
            move_down=move_down,
            enter_inspect=enter_inspect,
            raw_inspect=raw_inspect,
            quit_requested_path=runtime_files.quit_requested,
        )
        for key, commands in dashboard_bindings.items():
            self._bind_dashboard_key(tmux, DASHBOARD_KEY_TABLE, key, commands)
        self._bind_dashboard_key(
            tmux,
            ROOT_KEY_TABLE,
            "q",
            quit_dashboard_commands(
                session.session_name,
                runtime_files.quit_requested,
            ),
        )
        for mouse_key in LIVE_MOUSE_KEYS:
            self._bind_dashboard_key(
                tmux,
                DASHBOARD_KEY_TABLE,
                mouse_key,
                focus_commands("="),
            )

    def _install_inspect_bindings(
        self,
        tmux: TmuxSessionClient,
        runtime_files: RuntimeFiles,
        session: TmuxSessionTargets,
        exit_inspect: str,
        raw_inspect: str,
        formatted_inspect: str,
    ) -> None:
        inspect_bindings = inspect_key_bindings(
            right_pane_id=session.right_pane_id,
            exit_inspect=exit_inspect,
            raw_inspect=raw_inspect,
            formatted_inspect=formatted_inspect,
            quit_commands=quit_dashboard_commands(
                session.session_name,
                runtime_files.quit_requested,
            ),
        )
        for key, commands in inspect_bindings.items():
            self._bind_dashboard_key(tmux, INSPECT_KEY_TABLE, key, commands)

    def _apply_dashboard_copy_mode_bindings(
        self,
        tmux: TmuxSessionClient,
        runtime_files: RuntimeFiles,
        session: TmuxSessionTargets,
    ) -> None:
        if (
            self._move_up_command is None
            or self._move_down_command is None
            or self._enter_inspect_command is None
            or self._raw_inspect_command is None
        ):
            return

        dashboard_bindings = dashboard_key_bindings(
            session_name=session.session_name,
            left_pane_id=session.left_pane_id,
            move_up=self._move_up_command,
            move_down=self._move_down_command,
            enter_inspect=self._enter_inspect_command,
            raw_inspect=self._raw_inspect_command,
            quit_requested_path=runtime_files.quit_requested,
        )
        focus_mouse_target = focus_commands("=")
        for table in COPY_MODE_KEY_TABLES:
            for key, commands in dashboard_bindings.items():
                self._bind_dashboard_key(
                    tmux,
                    table,
                    key,
                    copy_mode_binding_commands(commands),
                )
            for mouse_key in LIVE_MOUSE_KEYS:
                self._bind_dashboard_key(
                    tmux,
                    table,
                    mouse_key,
                    copy_mode_binding_commands(focus_mouse_target),
                )

    def _apply_inspect_copy_mode_bindings(
        self,
        tmux: TmuxSessionClient,
        runtime_files: RuntimeFiles,
        session: TmuxSessionTargets,
    ) -> None:
        if (
            self._exit_inspect_command is None
            or self._raw_inspect_command is None
            or self._formatted_inspect_command is None
        ):
            return

        inspect_bindings = inspect_copy_mode_key_bindings(
            self._exit_inspect_command,
            raw_inspect=self._raw_inspect_command,
            formatted_inspect=self._formatted_inspect_command,
            quit_commands=quit_dashboard_commands(
                session.session_name,
                runtime_files.quit_requested,
            ),
        )
        for table in COPY_MODE_KEY_TABLES:
            for key, commands in inspect_bindings.items():
                self._bind_dashboard_key(tmux, table, key, commands)

    def _bind_dashboard_key(
        self,
        tmux: TmuxSessionClient,
        table: str,
        key: str,
        commands: list[list[str]],
    ) -> None:
        tmux.run(
            [
                "bind-key",
                "-T",
                table,
                key,
                "if-shell",
                "-F",
                "1",
                tmux_command_string(*commands),
            ]
        )
