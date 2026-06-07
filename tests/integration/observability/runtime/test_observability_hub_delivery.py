import unittest
from time import monotonic, sleep
from unittest.mock import patch

from orchestrator_cli.observability.runtime import ObservabilityHub
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    BlockingObserver,
    RecordingObserver,
    RequiredFailingObserver,
    RequiredStopFailObserver,
    RequiredStopTimeoutObserver,
    single_node_workflow,
)


class ObservabilityHubDeliveryTests(unittest.TestCase):
    def test_observability_hub_exposes_observer_stop_request(self) -> None:
        workflow = single_node_workflow()
        observer = RecordingObserver()
        observer.stop_requested = True  # type: ignore[attr-defined]

        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id="run-stop-request",
            observers=[observer],
            refresh_per_second=0,
        ) as hub:
            self.assertTrue(hub.stop_requested)

    def test_observability_hub_emit_does_not_block_on_blocked_observer(self) -> None:
        workflow = single_node_workflow()
        observer = BlockingObserver()

        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id="run-blocked-observer",
            observers=[observer],
            refresh_per_second=0,
        ) as hub:
            self.assertTrue(observer.entered.wait(timeout=1.0))
            started_at = monotonic()
            hub.emit(
                make_execution_event(
                    event_type="workflow_started",
                    workflow_name=workflow.name,
                    run_id="run-blocked-observer",
                )
            )
            self.assertLess(monotonic() - started_at, 0.1)
            observer.release.set()

    def test_observability_hub_coalesces_ticks_while_observer_is_blocked(
        self,
    ) -> None:
        workflow = single_node_workflow()
        observer = BlockingObserver()

        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id="run-coalesce-ticks",
            observers=[observer],
            refresh_per_second=100,
        ):
            self.assertTrue(observer.entered.wait(timeout=1.0))
            sleep(0.05)
            observer.release.set()

        self.assertLessEqual(len(observer.event_types), 3)

    def test_observability_hub_stop_request_survives_blocked_delivery(self) -> None:
        workflow = single_node_workflow()
        observer = BlockingObserver()

        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id="run-blocked-stop-request",
            observers=[observer],
            refresh_per_second=0,
        ) as hub:
            self.assertTrue(observer.entered.wait(timeout=1.0))
            observer.stop_requested = True  # type: ignore[attr-defined]
            self.assertTrue(hub.stop_requested)
            observer.release.set()

    def test_observability_hub_skips_stopped_observer_after_delivery_timeout(
        self,
    ) -> None:
        workflow = single_node_workflow()
        gate = BlockingObserver()
        healthy = RecordingObserver()

        with patch(
            "orchestrator_cli.observability.runtime.OBSERVER_DELIVERY_JOIN_TIMEOUT_SECONDS",
            0.01,
        ):
            with ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-skip-after-timeout",
                observers=[gate, healthy],
                refresh_per_second=0,
            ):
                self.assertTrue(gate.entered.wait(timeout=1.0))
            self.assertTrue(healthy.stopped)
            gate.release.set()
            sleep(0.05)

        self.assertEqual(healthy.event_types, [])

    def test_observability_hub_warns_when_required_observer_fails(self) -> None:
        workflow = single_node_workflow()
        healthy = RecordingObserver()
        warnings: list[str] = []
        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id="run-required-fail",
            observers=[RequiredFailingObserver(), healthy],
            refresh_per_second=0,
            warning_sink=warnings.append,
        ) as hub:
            hub.emit(
                make_execution_event(
                    event_type="workflow_started",
                    workflow_name=workflow.name,
                    run_id="run-required-fail",
                )
            )

        self.assertTrue(any("disabled" in warning for warning in warnings))
        self.assertTrue(healthy.stopped)

    def test_observability_hub_raises_for_required_observer_stop_failure(self) -> None:
        workflow = single_node_workflow()
        warnings: list[str] = []
        with (
            self.assertRaisesRegex(RuntimeError, "required stop failure"),
            ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-required-stop-fail",
                observers=[RequiredStopFailObserver()],
                refresh_per_second=0,
                warning_sink=warnings.append,
            ),
        ):
            pass
        self.assertTrue(any("stop failed" in warning for warning in warnings))

    def test_observability_hub_raises_for_required_observer_stop_timeout(self) -> None:
        workflow = single_node_workflow()
        warnings: list[str] = []
        with (
            patch(
                "orchestrator_cli.observability.runtime.OBSERVER_STOP_TIMEOUT_SECONDS",
                0.01,
            ),
            self.assertRaisesRegex(TimeoutError, "stop timed out"),
            ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id="run-required-stop-timeout",
                observers=[RequiredStopTimeoutObserver()],
                refresh_per_second=0,
                warning_sink=warnings.append,
            ),
        ):
            pass
        self.assertTrue(any("stop timed out" in warning for warning in warnings))
