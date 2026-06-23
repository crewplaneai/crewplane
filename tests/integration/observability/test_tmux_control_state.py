from __future__ import annotations

from crewplane.observability.tmux.control_state import (
    PaneGeometry,
    TmuxCompactControlState,
)
from crewplane.observability.tmux.runtime_files import (
    MODE_DASHBOARD,
    MODE_INSPECT,
)
from crewplane.observability.tmux.session import TmuxSessionTargets
from tests.integration.observability.tmux_fakes import FakeTmuxClient


def test_dashboard_resize_restores_key_table_and_left_focus() -> None:
    client = FakeTmuxClient()
    control_state = TmuxCompactControlState()

    assert control_state.recover_after_resize(
        client,
        MODE_DASHBOARD,
        tmux_targets(),
        PaneGeometry(80, 24, 100, 24),
    )

    commands = [args for args, _, _ in client.calls]
    assert ["send-keys", "-X", "-t", "%10", "cancel"] in commands
    assert ["send-keys", "-X", "-t", "%20", "cancel"] in commands
    assert [
        "set-option",
        "-t",
        "crewplane-test",
        "key-table",
        "crewplane-dashboard",
    ] in commands
    assert ["select-pane", "-t", "%10"] in commands


def test_inspect_resize_restores_inspect_table_and_right_focus() -> None:
    client = FakeTmuxClient()
    control_state = TmuxCompactControlState()

    assert control_state.recover_after_resize(
        client,
        MODE_INSPECT,
        tmux_targets(),
        PaneGeometry(80, 24, 100, 24),
    )

    commands = [args for args, _, _ in client.calls]
    assert [
        "set-option",
        "-t",
        "crewplane-test",
        "key-table",
        "crewplane-inspect",
    ] in commands
    assert ["select-pane", "-t", "%20"] in commands


def test_failed_resize_restore_is_retried() -> None:
    client = FakeTmuxClient()
    client.fail_next_key_table_restore = True
    control_state = TmuxCompactControlState()
    geometry = PaneGeometry(80, 24, 100, 24)

    assert not control_state.recover_after_resize(
        client,
        MODE_DASHBOARD,
        tmux_targets(),
        geometry,
    )
    client.calls.clear()

    assert control_state.recover_after_resize(
        client,
        MODE_DASHBOARD,
        tmux_targets(),
        geometry,
    )
    assert any(args[3] == "key-table" for args, _, _ in client.calls)


def tmux_targets() -> TmuxSessionTargets:
    return TmuxSessionTargets(
        session_name="crewplane-test",
        socket_name="socket",
        window_target="crewplane-test:dashboard",
        left_pane_id="%10",
        right_pane_id="%20",
    )
