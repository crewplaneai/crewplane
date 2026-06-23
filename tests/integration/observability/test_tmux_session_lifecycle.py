from __future__ import annotations

import gc
from pathlib import Path

import pytest

from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.tmux.session_lifecycle import (
    TmuxCompactSessionLifecycle,
)
from crewplane.observability.types import RunContext
from tests.helpers.observability import topology_from_workflow
from tests.integration.observability.tmux_fakes import FakeTmuxClient


def test_auto_close_false_preserves_runtime_files_after_stop_and_gc() -> None:
    client = FakeTmuxClient()
    lifecycle = TmuxCompactSessionLifecycle(
        auto_close_session=False,
        refresh_interval_seconds=1000.0,
        client_factory=client_factory(client),
    )
    session = lifecycle.create_session(run_context("preserve"))
    root = session.runtime_lease.root

    lifecycle.stop_session(session, auto_close_session=False)
    del lifecycle
    gc.collect()

    try:
        assert root.exists()
        assert session.runtime_files.left_content.exists()
    finally:
        session.runtime_lease.cleanup(force=True)


def test_auto_close_true_kills_session_and_cleans_runtime_files() -> None:
    client = FakeTmuxClient()
    lifecycle = TmuxCompactSessionLifecycle(
        auto_close_session=True,
        refresh_interval_seconds=1000.0,
        client_factory=client_factory(client),
    )
    session = lifecycle.create_session(run_context("close"))
    root = session.runtime_lease.root

    lifecycle.stop_session(session, auto_close_session=True)

    assert not root.exists()
    assert any(args[:2] == ["kill-session", "-t"] for args, _, _ in client.calls)


def test_failed_session_creation_forces_runtime_file_cleanup() -> None:
    client = EmptyRightPaneTmuxClient()
    lifecycle = TmuxCompactSessionLifecycle(
        auto_close_session=False,
        refresh_interval_seconds=1000.0,
        client_factory=client_factory(client),
    )

    with pytest.raises(RuntimeError):
        lifecycle.create_session(run_context("failed-create"))

    assert client.socket_name is not None
    leaked_roots = [
        path
        for path in Path("/tmp").glob("crewplane-tmux-compact-failed-create-*")
        if path.name == client.socket_name
    ]
    assert leaked_roots == []
    assert any(args[:2] == ["kill-session", "-t"] for args, _, _ in client.calls)


class EmptyRightPaneTmuxClient(FakeTmuxClient):
    def run(
        self,
        args: list[str],
        capture_output: bool = False,
        check: bool = True,
    ):
        if args[0] == "split-window" and capture_output:
            self.calls.append((args, capture_output, check))
            self.call_sockets.append(self.socket_name)
            return completed_process(args, stdout="")
        return super().run(args, capture_output=capture_output, check=check)


def completed_process(args: list[str], stdout: str):
    import subprocess

    return subprocess.CompletedProcess(["tmux", *args], 0, stdout=stdout, stderr="")


def client_factory(client: FakeTmuxClient):
    def create(socket_name: str | None) -> FakeTmuxClient:
        client.set_socket_name(socket_name)
        return client

    return create


def run_context(run_id: str) -> RunContext:
    return RunContext(
        workflow_topology=topology_from_workflow(
            WorkflowPlan(
                name="tmux.lifecycle",
                nodes=[
                    WorkflowNode(
                        id="node.a",
                        mode="parallel",
                        prompt_segments=[PromptSegment(role="shared", content="a")],
                        providers=[ProviderSpec(provider="alpha")],
                    )
                ],
            )
        ),
        run_id=run_id,
        refresh_per_second=0,
    )
