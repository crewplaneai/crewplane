import unittest
from time import monotonic
from unittest.mock import patch

from crewplane.observability.runtime import ObservabilityHub
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    BlockingObserver,
    BlockingStartObserver,
    CountingFailingObserver,
    DelayedStartObserver,
    FailingObserver,
    NoCleanupDelayedStartObserver,
    RecordingObserver,
    RequiredStartThreadFailureObserver,
    RequiredStopThreadFailureObserver,
    StartFailObserver,
    single_node_workflow,
)


class ObservabilityHubLifecycleTests(unittest.TestCase):
    def test_observability_hub_fanout_and_snapshot_state(self) -> None:
        workflow = single_node_workflow()
        observer = RecordingObserver()
        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id="run-1",
            observers=[observer],
            refresh_per_second=0,
        ) as hub:
            hub.emit(
                make_execution_event(
                    event_type="workflow_started",
                    workflow_name=workflow.name,
                    run_id="run-1",
                )
            )
            hub.emit(
                make_execution_event(
                    event_type="workflow_finished",
                    workflow_name=workflow.name,
                    run_id="run-1",
                )
            )

        self.assertTrue(observer.started)
        self.assertTrue(observer.stopped)
        self.assertEqual(observer.event_types[0], None)
        self.assertEqual(observer.workflow_statuses[-1], "succeeded")
        self.assertIn("workflow_started", observer.event_types)
        self.assertIn("workflow_finished", observer.event_types)

    def test_observability_hub_disables_failing_observer_and_continues(self) -> None:
        workflow = single_node_workflow()
        healthy = RecordingObserver()
        failing = FailingObserver()
        warnings: list[str] = []

        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id="run-2",
            observers=[failing, healthy],
            refresh_per_second=0,
            warning_sink=warnings.append,
        ) as hub:
            hub.emit(
                make_execution_event(
                    event_type="workflow_started",
                    workflow_name=workflow.name,
                    run_id="run-2",
                )
            )

        self.assertTrue(any("disabled" in warning for warning in warnings))
        self.assertGreaterEqual(len(healthy.event_types), 2)
        self.assertTrue(healthy.stopped)

    def test_observability_hub_skips_disabled_observer_in_queued_snapshots(
        self,
    ) -> None:
        workflow = single_node_workflow()
        gate = BlockingObserver()
        failing = CountingFailingObserver()
        healthy = RecordingObserver()

        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id="run-skip-disabled",
            observers=[gate, failing, healthy],
            refresh_per_second=0,
        ) as hub:
            self.assertTrue(gate.entered.wait(timeout=1.0))
            hub.emit(
                make_execution_event(
                    event_type="workflow_started",
                    workflow_name=workflow.name,
                    run_id="run-skip-disabled",
                )
            )
            hub.emit(
                make_execution_event(
                    event_type="workflow_finished",
                    workflow_name=workflow.name,
                    run_id="run-skip-disabled",
                )
            )
            gate.release.set()

        self.assertEqual(failing.call_count, 1)
        self.assertIn("workflow_finished", healthy.event_types)

    def test_observability_hub_continues_when_observer_start_fails(self) -> None:
        workflow = single_node_workflow()
        warnings: list[str] = []
        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id="run-start-fail",
            observers=[StartFailObserver()],
            refresh_per_second=0,
            warning_sink=warnings.append,
        ) as hub:
            self.assertEqual(hub.active_observer_count, 0)
            hub.emit(
                make_execution_event(
                    event_type="workflow_started",
                    workflow_name=workflow.name,
                    run_id="run-start-fail",
                )
            )
        self.assertTrue(any("start failed" in warning for warning in warnings))

    def test_observability_hub_continues_when_observer_start_blocks(self) -> None:
        workflow = single_node_workflow()
        observer = BlockingStartObserver()
        warnings: list[str] = []

        with patch(
            "crewplane.observability.runtime.OBSERVER_START_TIMEOUT_SECONDS",
            0.01,
        ):
            started_at = monotonic()
            with ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-start-blocked",
                observers=[observer],
                refresh_per_second=0,
                warning_sink=warnings.append,
            ) as hub:
                self.assertEqual(hub.active_observer_count, 0)

        self.assertLess(monotonic() - started_at, 0.1)
        self.assertTrue(observer.entered.is_set())
        self.assertTrue(any("start timed out" in warning for warning in warnings))

    def test_observability_hub_cleans_up_observer_that_starts_after_timeout(
        self,
    ) -> None:
        workflow = single_node_workflow()
        observer = DelayedStartObserver()

        with (
            patch(
                "crewplane.observability.runtime.OBSERVER_START_TIMEOUT_SECONDS",
                0.01,
            ),
            ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-delayed-start",
                observers=[observer],
                refresh_per_second=0,
            ) as hub,
        ):
            self.assertTrue(observer.entered.wait(timeout=1.0))
            self.assertEqual(hub.active_observer_count, 0)
            observer.release.set()

        self.assertTrue(observer.cleaned_up.wait(timeout=1.0))

    def test_observability_hub_respects_start_timeout_cleanup_opt_out(self) -> None:
        workflow = single_node_workflow()
        observer = NoCleanupDelayedStartObserver()

        with (
            patch(
                "crewplane.observability.runtime.OBSERVER_START_TIMEOUT_SECONDS",
                0.01,
            ),
            ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-delayed-start-no-cleanup",
                observers=[observer],
                refresh_per_second=0,
            ) as hub,
        ):
            self.assertTrue(observer.entered.wait(timeout=1.0))
            self.assertEqual(hub.active_observer_count, 0)
            observer.release.set()

        self.assertFalse(observer.cleaned_up.wait(timeout=0.05))

    def test_observability_hub_continues_when_start_thread_fails(self) -> None:
        workflow = single_node_workflow()
        warnings: list[str] = []

        class StartFailureThread:
            def __init__(
                self,
                target,  # noqa: ARG002 - Required by Thread test double.
                args=(),  # noqa: ARG002 - Required by Thread test double.
                name=None,
                daemon=False,  # noqa: ARG002 - Required by Thread test double.
            ):
                self.name = name

            def start(self) -> None:
                if str(self.name).startswith("crewplane-observer-start"):
                    raise RuntimeError("thread unavailable")

            def join(self, timeout=None) -> None:  # noqa: ARG002 - Thread test double.
                pass

            def is_alive(self) -> bool:
                return False

        with (
            patch("crewplane.observability.runtime.Thread", StartFailureThread),
            ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-start-thread-fail",
                observers=[RecordingObserver()],
                refresh_per_second=0,
                warning_sink=warnings.append,
            ) as hub,
        ):
            self.assertEqual(hub.active_observer_count, 0)

        self.assertTrue(any("start thread failed" in warning for warning in warnings))

    def test_observability_hub_raises_for_required_observer_start_thread_failure(
        self,
    ) -> None:
        workflow = single_node_workflow()
        warnings: list[str] = []

        class StartFailureThread:
            def __init__(
                self,
                target,  # noqa: ARG002 - Required by Thread test double.
                args=(),  # noqa: ARG002 - Required by Thread test double.
                name=None,
                daemon=False,  # noqa: ARG002 - Required by Thread test double.
            ):
                self.name = name

            def start(self) -> None:
                if str(self.name).startswith("crewplane-observer-start"):
                    raise RuntimeError("thread unavailable")

            def join(self, timeout=None) -> None:  # noqa: ARG002 - Thread test double.
                pass

            def is_alive(self) -> bool:
                return False

        with (
            patch("crewplane.observability.runtime.Thread", StartFailureThread),
            self.assertRaisesRegex(RuntimeError, "thread unavailable"),
            ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-required-start-thread-fail",
                observers=[RequiredStartThreadFailureObserver()],
                refresh_per_second=0,
                warning_sink=warnings.append,
            ),
        ):
            pass

        self.assertTrue(any("start thread failed" in warning for warning in warnings))

    def test_observability_hub_continues_when_delivery_worker_start_fails(
        self,
    ) -> None:
        workflow = single_node_workflow()
        observer = RecordingObserver()
        warnings: list[str] = []

        class DeliveryStartFailureThread:
            def __init__(
                self,
                target,  # noqa: ARG002 - Required by Thread test double.
                args=(),  # noqa: ARG002 - Required by Thread test double.
                name=None,
                daemon=False,  # noqa: ARG002 - Required by Thread test double.
            ):
                self.name = name

            def start(self) -> None:
                if self.name == "crewplane-observability-delivery":
                    raise RuntimeError("thread unavailable")

            def join(self, timeout=None) -> None:  # noqa: ARG002 - Thread test double.
                pass

            def is_alive(self) -> bool:
                return False

        with (
            patch(
                "crewplane.observability.runtime.Thread",
                DeliveryStartFailureThread,
            ),
            ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-delivery-thread-fail",
                observers=[observer],
                refresh_per_second=0,
                warning_sink=warnings.append,
            ) as hub,
        ):
            hub.emit(
                make_execution_event(
                    event_type="workflow_started",
                    workflow_name=workflow.name,
                    run_id="run-delivery-thread-fail",
                )
            )

        self.assertTrue(
            any("delivery worker failed to start" in warning for warning in warnings)
        )
        self.assertIn("workflow_started", observer.event_types)

    def test_observability_hub_continues_when_ticker_start_fails(self) -> None:
        workflow = single_node_workflow()
        warnings: list[str] = []

        class TickerStartFailureThread:
            def __init__(
                self,
                target,  # noqa: ARG002 - Required by Thread test double.
                args=(),  # noqa: ARG002 - Required by Thread test double.
                name=None,
                daemon=False,  # noqa: ARG002 - Required by Thread test double.
            ):
                self.name = name

            def start(self) -> None:
                if self.name == "crewplane-observability-ticker":
                    raise RuntimeError("thread unavailable")

            def join(self, timeout=None) -> None:  # noqa: ARG002 - Thread test double.
                pass

            def is_alive(self) -> bool:
                return False

        with (
            patch(
                "crewplane.observability.runtime.Thread",
                TickerStartFailureThread,
            ),
            ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-ticker-thread-fail",
                observers=[],
                refresh_per_second=1,
                warning_sink=warnings.append,
            ) as hub,
        ):
            hub.emit(
                make_execution_event(
                    event_type="workflow_started",
                    workflow_name=workflow.name,
                    run_id="run-ticker-thread-fail",
                )
            )

        self.assertTrue(
            any("ticker failed to start" in warning for warning in warnings)
        )

    def test_observability_hub_continues_when_stop_thread_fails(self) -> None:
        workflow = single_node_workflow()
        warnings: list[str] = []

        class StopFailureThread:
            def __init__(
                self,
                target,
                args=(),
                name=None,
                daemon=False,  # noqa: ARG002 - Required by Thread test double.
            ):
                self._target = target
                self._args = args
                self.name = name

            def start(self) -> None:
                if str(self.name).startswith("crewplane-observer-stop"):
                    raise RuntimeError("thread unavailable")
                if str(self.name).startswith("crewplane-observer-start"):
                    self._target(*self._args)

            def join(self, timeout=None) -> None:  # noqa: ARG002 - Thread test double.
                pass

            def is_alive(self) -> bool:
                return False

        with (
            patch("crewplane.observability.runtime.Thread", StopFailureThread),
            ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-stop-thread-fail",
                observers=[RecordingObserver()],
                refresh_per_second=0,
                warning_sink=warnings.append,
            ),
        ):
            pass

        self.assertTrue(any("stop thread failed" in warning for warning in warnings))

    def test_observability_hub_raises_for_required_observer_stop_thread_failure(
        self,
    ) -> None:
        workflow = single_node_workflow()
        warnings: list[str] = []

        class StopFailureThread:
            def __init__(
                self,
                target,
                args=(),
                name=None,
                daemon=False,  # noqa: ARG002 - Required by Thread test double.
            ):
                self._target = target
                self._args = args
                self.name = name

            def start(self) -> None:
                if str(self.name).startswith("crewplane-observer-stop"):
                    raise RuntimeError("thread unavailable")
                if str(self.name).startswith("crewplane-observer-start"):
                    self._target(*self._args)

            def join(self, timeout=None) -> None:  # noqa: ARG002 - Thread test double.
                pass

            def is_alive(self) -> bool:
                return False

        with (
            patch("crewplane.observability.runtime.Thread", StopFailureThread),
            self.assertRaisesRegex(RuntimeError, "thread unavailable"),
            ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-required-stop-thread-fail",
                observers=[RequiredStopThreadFailureObserver()],
                refresh_per_second=0,
                warning_sink=warnings.append,
            ),
        ):
            pass

        self.assertTrue(any("stop thread failed" in warning for warning in warnings))
