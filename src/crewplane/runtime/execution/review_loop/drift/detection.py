"""Coordinate drift checks after a guarded provider call."""

from __future__ import annotations

from ..types import (
    DriftCheckResult,
    DriftGuardCallRequest,
    DriftMonitoringWindow,
    EventLogAppendCapture,
)
from .comparison import merge_drift_results, should_check_shared_reserved_drift
from .node import detect_node_drift
from .reserved import (
    detect_event_log_append_drift,
    detect_shared_reserved_drift,
    detect_summary_drift,
)

__all__ = ["detect_provider_call_drift"]


def detect_provider_call_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    event_log_capture: EventLogAppendCapture | None,
    event_log_start_index: int,
) -> DriftCheckResult:
    check_shared_reserved_drift = should_check_shared_reserved_drift(
        monitoring_window.activity_window,
        request.telemetry,
        request.node.id,
    )
    return merge_drift_results(
        detect_node_drift(request, monitoring_window),
        detect_shared_reserved_drift(
            request,
            monitoring_window,
        ),
        detect_summary_drift(request, monitoring_window),
        detect_event_log_append_drift(
            request,
            monitoring_window,
            event_log_capture,
            event_log_start_index,
            check_shared_reserved_drift,
        ),
    )
