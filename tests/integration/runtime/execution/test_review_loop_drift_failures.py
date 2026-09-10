import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.review_loop.drift import (
    guard as review_loop_drift_guard,
)
from crewplane.runtime.execution.review_loop.types import (
    DriftGuardCallRequest,
)
from tests.integration.runtime.execution.review_loop_drift_support import (
    make_drift_request,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    provider_failure,
)


def test_drift_is_checked_when_provider_call_fails(tmp_path: Path) -> None:
    request, _output, node_dir = make_drift_request(tmp_path)

    class MutatingFailingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,  # noqa: ARG002 - Required by protocol.
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            mutation_path = node_dir / "review-state" / "mutated-after-failure.md"
            mutation_path.parent.mkdir(parents=True, exist_ok=True)
            mutation_path.write_text("mutated", encoding="utf-8")
            raise RuntimeError("provider boom")

    request = DriftGuardCallRequest(
        runtime_context=request.runtime_context,
        output=request.output,
        node=request.node,
        node_dir=request.node_dir,
        invoker=MutatingFailingInvoker(),
        telemetry=request.telemetry,
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        provider=request.provider,
        task_id=request.task_id,
        prompt=request.prompt,
        output_file=request.output_file,
        role_label=request.role_label,
        findings_enabled=request.findings_enabled,
        allowed_paths=request.allowed_paths,
        display=request.display,
    )

    with pytest.raises(RuntimeError, match="provider boom") as exc_info:
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("artifact drift detected" in note for note in notes)


def test_fatal_drift_after_provider_call_failure_raises_fatal_error(
    tmp_path: Path,
) -> None:
    request, output, _node_dir = make_drift_request(tmp_path)

    class MutatingFailingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,  # noqa: ARG002 - Required by protocol.
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            result_path = output.results_dir / "review.node-result.md"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text("mutated", encoding="utf-8")
            raise RuntimeError("provider boom")

    request = DriftGuardCallRequest(
        runtime_context=request.runtime_context,
        output=request.output,
        node=request.node,
        node_dir=request.node_dir,
        invoker=MutatingFailingInvoker(),
        telemetry=request.telemetry,
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        provider=request.provider,
        task_id=request.task_id,
        prompt=request.prompt,
        output_file=request.output_file,
        role_label=request.role_label,
        findings_enabled=request.findings_enabled,
        allowed_paths=request.allowed_paths,
        display=request.display,
    )

    with pytest.raises(NodeExecutionError, match="fatal artifacts") as exc_info:
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))

    assert isinstance(exc_info.value.__cause__, InvocationFailureError)
    assert "provider boom" in str(exc_info.value.__cause__)
    notes = getattr(exc_info.value.__cause__, "__notes__", [])
    assert any("1 fatal path" in note for note in notes)


def test_drift_detection_defect_after_expected_provider_failure_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = make_drift_request(tmp_path)

    class FailingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,  # noqa: ARG002 - Required by protocol.
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            raise provider_failure("expected provider failure")

    monkeypatch.setattr(
        review_loop_drift_guard,
        "detect_provider_call_drift",
        Mock(side_effect=TypeError("simulated drift detector defect")),
    )
    request = DriftGuardCallRequest(
        runtime_context=request.runtime_context,
        output=request.output,
        node=request.node,
        node_dir=request.node_dir,
        invoker=FailingInvoker(),
        telemetry=request.telemetry,
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        provider=request.provider,
        task_id=request.task_id,
        prompt=request.prompt,
        output_file=request.output_file,
        role_label=request.role_label,
        findings_enabled=request.findings_enabled,
        allowed_paths=request.allowed_paths,
        display=request.display,
    )

    with pytest.raises(TypeError, match="simulated drift detector defect") as exc_info:
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))

    assert type(exc_info.value) is TypeError
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("expected provider failure" in note for note in notes)


def test_drift_detection_defect_preserves_unexpected_provider_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = make_drift_request(tmp_path)
    original_cause = ValueError("original provider cause")

    class FailingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,  # noqa: ARG002 - Required by protocol.
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            raise TypeError("provider defect") from original_cause

    monkeypatch.setattr(
        review_loop_drift_guard,
        "detect_provider_call_drift",
        Mock(side_effect=RuntimeError("simulated drift detector defect")),
    )
    request = DriftGuardCallRequest(
        runtime_context=request.runtime_context,
        output=request.output,
        node=request.node,
        node_dir=request.node_dir,
        invoker=FailingInvoker(),
        telemetry=request.telemetry,
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        provider=request.provider,
        task_id=request.task_id,
        prompt=request.prompt,
        output_file=request.output_file,
        role_label=request.role_label,
        findings_enabled=request.findings_enabled,
        allowed_paths=request.allowed_paths,
        display=request.display,
    )

    with pytest.raises(TypeError, match="provider defect") as exc_info:
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))

    assert exc_info.value.__cause__ is original_cause
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("artifact drift detection failed" in note for note in notes)


def test_drift_detection_defect_preserves_unexpected_provider_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = make_drift_request(tmp_path)
    original_context = ValueError("original provider context")

    class FailingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,  # noqa: ARG002 - Required by protocol.
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            try:
                raise original_context
            except ValueError:
                raise TypeError("provider defect")  # noqa: B904 - Regression covers implicit context preservation.

    monkeypatch.setattr(
        review_loop_drift_guard,
        "detect_provider_call_drift",
        Mock(side_effect=RuntimeError("simulated drift detector defect")),
    )
    request = DriftGuardCallRequest(
        runtime_context=request.runtime_context,
        output=request.output,
        node=request.node,
        node_dir=request.node_dir,
        invoker=FailingInvoker(),
        telemetry=request.telemetry,
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        provider=request.provider,
        task_id=request.task_id,
        prompt=request.prompt,
        output_file=request.output_file,
        role_label=request.role_label,
        findings_enabled=request.findings_enabled,
        allowed_paths=request.allowed_paths,
        display=request.display,
    )

    with pytest.raises(TypeError, match="provider defect") as exc_info:
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))

    assert exc_info.value.__context__ is original_context
    assert exc_info.value.__suppress_context__ is False
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("artifact drift detection failed" in note for note in notes)
