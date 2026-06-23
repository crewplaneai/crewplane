from __future__ import annotations

from time import sleep

import pytest

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.runtime import ObservabilityHub
from crewplane.observability.tmux.compact import TmuxCompactRuntime
from crewplane.observability.tmux.refresh import RefreshOutcome
from crewplane.observability.tmux.window import TmuxCompactWindowOptions
from crewplane.observability.types import RunContext, RunResult
from tests.helpers.observability import topology_from_workflow
from tests.integration.observability.tmux_fakes import (
    FakeCompactSessionLifecycle,
    FakeTmuxClient,
)


def test_create_session_failure_is_downgraded_by_observability_hub() -> None:
    warnings: list[str] = []
    lifecycle = fake_lifecycle(auto_close_session=True)
    lifecycle.create_failure = RuntimeError("create failed")
    runtime = TmuxCompactRuntime(lifecycle=lifecycle)

    with ObservabilityHub(
        workflow_topology=topology_from_workflow(workflow_plan()),
        run_id="startup-create-failure",
        observers=[runtime],
        refresh_per_second=0,
        warning_sink=warnings.append,
    ) as hub:
        assert hub.active_observer_count == 0

    assert any("create failed" in warning for warning in warnings)


def test_window_configure_failure_rolls_back_session() -> None:
    lifecycle = fake_lifecycle(auto_close_session=False)
    runtime = TmuxCompactRuntime(
        auto_close_session=False,
        lifecycle=lifecycle,
        window=FailingWindow(),
    )

    with pytest.raises(RuntimeError, match="window failed"):
        runtime.start(run_context("startup-window-failure"))

    assert lifecycle.session is None
    assert lifecycle.last_session is not None
    assert not lifecycle.last_session.runtime_lease.root.exists()


def test_binding_install_failure_rolls_back_session() -> None:
    lifecycle = fake_lifecycle(auto_close_session=False)
    runtime = TmuxCompactRuntime(
        auto_close_session=False,
        lifecycle=lifecycle,
        bindings=FailingBindings(),
        refresh_controller=NoopRefreshController(),
    )

    with pytest.raises(RuntimeError, match="bindings failed"):
        runtime.start(run_context("startup-bindings-failure"))

    assert lifecycle.session is None
    assert lifecycle.last_session is not None
    assert not lifecycle.last_session.runtime_lease.root.exists()


def test_attach_failure_rolls_back_session() -> None:
    lifecycle = fake_lifecycle(auto_close_session=False)
    lifecycle.attach_failure = RuntimeError("attach failed")
    runtime = TmuxCompactRuntime(auto_close_session=False, lifecycle=lifecycle)

    with pytest.raises(RuntimeError, match="attach failed"):
        runtime.start(run_context("startup-attach-failure"))

    assert lifecycle.session is None
    assert lifecycle.last_session is not None
    assert not lifecycle.last_session.runtime_lease.root.exists()


def test_refresh_loop_warns_and_continues_after_refresh_exception() -> None:
    warnings: list[str] = []
    refresh = FailingOnceRefreshController()
    lifecycle = fake_lifecycle(auto_close_session=True, warning_sink=warnings.append)
    runtime = TmuxCompactRuntime(
        auto_close_session=True,
        warning_sink=warnings.append,
        refresh_interval_seconds=0.1,
        lifecycle=lifecycle,
        refresh_controller=refresh,
    )

    runtime.start(run_context("refresh-loop-warning"))
    try:
        deadline = 20
        while refresh.call_count < 2 and deadline > 0:
            sleep(0.05)
            deadline -= 1
    finally:
        runtime.stop(RunResult(status="succeeded"))

    assert refresh.call_count >= 2
    assert any(
        "tmux compact refresh failed: refresh failed" in item for item in warnings
    )


class FailingWindow(TmuxCompactWindowOptions):
    def configure(  # type: ignore[no-untyped-def]
        self,
        tmux,  # noqa: ARG002 - Required by window protocol.
        session,  # noqa: ARG002 - Required by window protocol.
    ) -> None:
        raise RuntimeError("window failed")


class FailingBindings:
    def reset(self) -> None:
        pass

    def install(  # type: ignore[no-untyped-def]
        self,
        tmux,  # noqa: ARG002 - Required by bindings protocol.
        runtime_files,  # noqa: ARG002 - Required by bindings protocol.
        session,  # noqa: ARG002 - Required by bindings protocol.
    ) -> None:
        raise RuntimeError("bindings failed")

    def sync_copy_mode_bindings(  # type: ignore[no-untyped-def]
        self,
        tmux,  # noqa: ARG002 - Required by bindings protocol.
        runtime_files,  # noqa: ARG002 - Required by bindings protocol.
        session,  # noqa: ARG002 - Required by bindings protocol.
        mode,  # noqa: ARG002 - Required by bindings protocol.
    ) -> None:
        pass


class NoopRefreshController:
    def reset(self) -> None:
        pass

    def on_snapshot(self, event, snapshot) -> None:  # type: ignore[no-untyped-def]
        pass

    def refresh_once(  # type: ignore[no-untyped-def]
        self,
        session,  # noqa: ARG002 - Required by refresh protocol.
    ) -> RefreshOutcome:
        return RefreshOutcome()


class FailingOnceRefreshController(NoopRefreshController):
    def __init__(self) -> None:
        self.call_count = 0

    def refresh_once(  # type: ignore[no-untyped-def]
        self,
        session,  # noqa: ARG002 - Required by refresh protocol.
    ) -> RefreshOutcome:
        self.call_count += 1
        if self.call_count == 1:
            raise RuntimeError("refresh failed")
        return RefreshOutcome()


def fake_lifecycle(
    auto_close_session: bool,
    warning_sink=None,  # type: ignore[no-untyped-def]
) -> FakeCompactSessionLifecycle:
    return FakeCompactSessionLifecycle(
        auto_close_session=auto_close_session,
        client=FakeTmuxClient(),
        warning_sink=warning_sink,
    )


def run_context(run_id: str) -> RunContext:
    return RunContext(
        workflow_topology=topology_from_workflow(workflow_plan()),
        run_id=run_id,
        refresh_per_second=0,
    )


def workflow_plan() -> WorkflowPlan:
    return WorkflowPlan(
        name="startup.workflow",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                ],
                providers=[ProviderSpec(provider="alpha")],
            )
        ],
    )
