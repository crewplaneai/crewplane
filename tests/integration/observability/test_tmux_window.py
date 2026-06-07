from __future__ import annotations

from orchestrator_cli.observability.tmux.session import TmuxSessionTargets
from orchestrator_cli.observability.tmux.window import TmuxCompactWindowOptions
from tests.integration.observability.tmux_fakes import FakeTmuxClient


def test_status_cache_updates_only_after_successful_option_write() -> None:
    client = FakeTmuxClient()
    window = TmuxCompactWindowOptions()
    client.fail_status_option = True

    window.set_status_option_if_changed(
        client,
        "orchestrator-test",
        "status-left",
        "left",
    )
    window.set_status_option_if_changed(
        client,
        "orchestrator-test",
        "status-left",
        "left",
    )

    assert status_left_write_count(client) == 2

    client.fail_status_option = False
    window.set_status_option_if_changed(
        client,
        "orchestrator-test",
        "status-left",
        "left",
    )
    window.set_status_option_if_changed(
        client,
        "orchestrator-test",
        "status-left",
        "left",
    )

    assert status_left_write_count(client) == 3


def test_pane_title_cache_resets_per_session() -> None:
    client = FakeTmuxClient()
    window = TmuxCompactWindowOptions()
    session = TmuxSessionTargets(
        session_name="orchestrator-test",
        socket_name="socket",
        window_target="orchestrator-test:dashboard",
        left_pane_id="%10",
        right_pane_id="%20",
    )

    window.configure(client, session)
    first_count = pane_title_write_count(client)
    window.configure(client, session)
    second_count = pane_title_write_count(client)
    window.reset()
    window.configure(client, session)

    assert first_count == 2
    assert second_count == 2
    assert pane_title_write_count(client) == 4


def status_left_write_count(client: FakeTmuxClient) -> int:
    return sum(
        1
        for args, _, _ in client.calls
        if len(args) >= 5 and args[0] == "set-option" and args[3] == "status-left"
    )


def pane_title_write_count(client: FakeTmuxClient) -> int:
    return sum(
        1
        for args, _, _ in client.calls
        if len(args) >= 6
        and args[:3] == ["set-option", "-p", "-t"]
        and args[4] == "@orchestrator_title"
    )
