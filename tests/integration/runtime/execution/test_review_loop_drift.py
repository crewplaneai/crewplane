import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.architecture.contracts import (
    InvocationProcessEvent,
    NodeArtifactRequest,
)
from crewplane.architecture.ports import (
    ProviderProcessInvocation,
    ProviderProcessPublication,
)
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.review_loop.drift import (
    capture as review_loop_drift_capture,
)
from crewplane.runtime.execution.review_loop.drift import (
    comparison as review_loop_drift_comparison,
)
from crewplane.runtime.execution.review_loop.drift import (
    guard as review_loop_drift_guard,
)
from crewplane.runtime.execution.review_loop.drift import (
    recovery as review_loop_drift_recovery,
)
from crewplane.runtime.execution.review_loop.drift import (
    reserved as review_loop_drift_reserved,
)
from crewplane.runtime.execution.review_loop.types import (
    ActivityWindow,
    DriftMonitoringWindow,
)
from tests.integration.runtime.execution.review_loop_drift_support import (
    make_drift_request,
    recovery_payload,
)


def test_current_invocation_and_parallel_reviewer_outputs_are_allowed(
    tmp_path: Path,
) -> None:
    _, output, node_dir = make_drift_request(tmp_path)
    executor_output = node_dir / "exec_executor_0_round1.md"
    reviewer_output = node_dir / "review_reviewer_0_round1.md"

    drift = review_loop_drift_comparison.detect_artifact_drift(
        before_snapshot={},
        after_snapshot={executor_output: (1, "a"), reviewer_output: (1, "b")},
        allowed_paths={executor_output, reviewer_output},
        output=output,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_cli_provider_process_state_is_an_expected_runtime_publication(
    tmp_path: Path,
) -> None:
    request, output, _node_dir = make_drift_request(tmp_path, use_cli_invoker=True)
    request.allowed_paths.add(request.output_file)

    warning_count = asyncio.run(
        review_loop_drift_guard.run_provider_call_with_drift_guard(request)
    )

    process_states = tuple(
        (output.stages_dir / "manifests" / "provider-processes").glob("*.json")
    )
    assert warning_count == 0
    assert len(process_states) == 1
    assert process_states[0] not in request.allowed_paths
    assert request.runtime_publications is not None
    published, _ = request.runtime_publications.snapshot()
    assert process_states[0] in published
    assert (
        recovery_payload(
            request.runtime_publications,
            process_states[0],
        )
        == process_states[0].read_bytes()
    )
    request.runtime_publications.close()


def test_parallel_cli_provider_process_states_are_expected_publications(
    tmp_path: Path,
) -> None:
    first, output, node_dir = make_drift_request(tmp_path, use_cli_invoker=True)
    session = review_loop_drift_guard.create_drift_guard_session(None)
    first_output = node_dir / "exec_executor_0_round1.md"
    second_output = node_dir / "exec_executor_1_round1.md"
    allowed_paths = {first_output, second_output}
    first.drift_session = session
    first.allowed_paths = allowed_paths
    second = replace(
        first,
        provider=first.provider.model_copy(update={"task_id": "exec_executor_1"}),
        task_id="exec_executor_1",
        output_file=second_output,
        runtime_publications=None,
    )

    async def run_parallel_calls() -> tuple[int, int]:
        results = await asyncio.gather(
            review_loop_drift_guard.run_provider_call_with_drift_guard(first),
            review_loop_drift_guard.run_provider_call_with_drift_guard(second),
        )
        return results[0], results[1]

    warning_counts = asyncio.run(run_parallel_calls())

    process_states = tuple(
        (output.stages_dir / "manifests" / "provider-processes").glob("*.json")
    )
    assert warning_counts == (0, 0)
    assert len(process_states) == 2


def test_cli_provider_process_state_tampering_remains_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, output, _node_dir = make_drift_request(tmp_path, use_cli_invoker=True)
    request.allowed_paths.add(request.output_file)
    write_process_event = output.write_provider_process_event

    def write_process_event_then_tamper(
        invocation: ProviderProcessInvocation,
        event: InvocationProcessEvent,
    ) -> ProviderProcessPublication:
        publication = write_process_event(invocation, event)
        if event.status == "exited":
            payload = json.loads(publication.path.read_text(encoding="utf-8"))
            payload["returncode"] = 99
            publication.path.write_text(json.dumps(payload), encoding="utf-8")
        return publication

    monkeypatch.setattr(
        output,
        "write_provider_process_event",
        write_process_event_then_tamper,
    )

    with pytest.raises(NodeExecutionError, match="modified fatal artifacts"):
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))


def test_preexisting_shared_reserved_drift_is_fatal_when_not_exclusive(
    tmp_path: Path,
) -> None:
    request, output, _ = make_drift_request(tmp_path)
    result_path = output.results_dir / "review.node-result.md"
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot={result_path: (1, "before")},
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=1),
    )

    drift = review_loop_drift_reserved.detect_shared_reserved_drift(
        request,
        window,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == (result_path,)


def test_new_reserved_drift_is_fatal_when_not_exclusive(tmp_path: Path) -> None:
    request, output, _ = make_drift_request(tmp_path)
    result_path = output.results_dir / "other-node-result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("unattributed", encoding="utf-8")
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot={},
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=1),
    )

    drift = review_loop_drift_reserved.detect_shared_reserved_drift(
        request,
        window,
    )

    assert drift.fatal_paths == (result_path,)


@pytest.mark.parametrize(
    "target_kind",
    ["candidate", "peer_verdict", "result", "findings", "manifest"],
)
def test_drift_guard_rejects_and_restores_producer_owned_artifacts(
    tmp_path: Path,
    target_kind: str,
) -> None:
    request, output, node_dir = make_drift_request(tmp_path)
    targets = {
        "candidate": request.output_file,
        "peer_verdict": node_dir / "peer_reviewer_0_round1.md",
        "result": output.results_dir / "review.node-result.md",
        "findings": output.results_dir / "review.node-findings.md",
        "manifest": output.stages_dir / "manifests" / "run.json",
    }
    target = targets[target_kind]
    target.parent.mkdir(parents=True, exist_ok=True)
    original = f"original {target_kind}".encode()
    target.write_bytes(original)

    class MutatingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            target.write_text("provider mutation", encoding="utf-8")
            output_file.write_text("provider output", encoding="utf-8")

    request.invoker = MutatingInvoker()

    with pytest.raises((NodeExecutionError, RuntimeError)):
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))

    assert target.read_bytes() == original


@pytest.mark.parametrize("target_kind", ["summary", "events"])
def test_drift_guard_restores_unsafe_strict_log_substitution(
    tmp_path: Path,
    target_kind: str,
) -> None:
    request, output, _node_dir = make_drift_request(tmp_path)
    target = (
        output.get_run_summary_path()
        if target_kind == "summary"
        else output.get_run_event_log_path()
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    original = f"original {target_kind}\n".encode()
    target.write_bytes(original)
    outside = tmp_path / f"outside-{target_kind}.txt"
    outside.write_text("outside\n", encoding="utf-8")

    class UnsafeLogMutatingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            target.unlink()
            target.symlink_to(outside)
            output_file.write_text("provider output", encoding="utf-8")

    request.invoker = UnsafeLogMutatingInvoker()

    try:
        with pytest.raises(NodeExecutionError, match="modified fatal artifacts"):
            asyncio.run(
                review_loop_drift_guard.run_provider_call_with_drift_guard(request)
            )

        assert not target.is_symlink()
        assert target.read_bytes() == original
    finally:
        request.runtime_context.runtime_publications.close()


def test_drift_guard_removes_forged_unpublished_peer_output(
    tmp_path: Path,
) -> None:
    request, _output, node_dir = make_drift_request(tmp_path)
    peer_output = node_dir / "peer_reviewer_0_round1.md"
    request.protected_paths.add(peer_output)
    request.allowed_paths.add(request.output_file)

    class PeerMutatingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            peer_output.write_text("forged approval", encoding="utf-8")
            output_file.write_text("provider output", encoding="utf-8")

    request.invoker = PeerMutatingInvoker()

    with pytest.raises(NodeExecutionError, match="modified fatal artifacts"):
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))

    assert not peer_output.exists()


def test_nested_stage_drift_guard_rejects_and_restores_run_manifest(
    tmp_path: Path,
) -> None:
    request, output, _node_dir = make_drift_request(tmp_path)
    nested_contract = request.node.artifact_contract.model_copy(
        update={"stage_path": "custom/build-stage"}
    )
    nested_node = request.node.model_copy(update={"artifact_contract": nested_contract})
    nested_dir = output.create_node_dir(
        NodeArtifactRequest(nested_node.id, nested_contract)
    )
    request.node = nested_node
    request.node_dir = nested_dir
    request.output_file = nested_dir / "exec_executor_0_round1.md"
    request.allowed_paths.add(request.output_file)
    manifest_path = output.stages_dir / "manifests" / "run.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"status":"running"}\n'
    manifest_path.write_bytes(original)

    class ManifestMutatingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            manifest_path.write_text('{"status":"succeeded"}\n', encoding="utf-8")
            output_file.write_text("provider output", encoding="utf-8")

    request.invoker = ManifestMutatingInvoker()

    with pytest.raises(NodeExecutionError, match="modified fatal artifacts"):
        asyncio.run(review_loop_drift_guard.run_provider_call_with_drift_guard(request))

    assert manifest_path.read_bytes() == original


def test_reserved_file_replaced_by_directory_is_restored(
    tmp_path: Path,
) -> None:
    request, output, _node_dir = make_drift_request(tmp_path)
    target = output.results_dir / "protected-result.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"original result")
    monitoring_window = review_loop_drift_capture.capture_drift_monitoring_window(
        node_id=request.node.id,
        node_dir=request.node_dir,
        output=output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
    )

    target.unlink()
    target.mkdir()
    (target / "forged.md").write_text("forged", encoding="utf-8")

    drift = review_loop_drift_reserved.detect_shared_reserved_drift(
        request,
        monitoring_window,
    )

    assert target in drift.fatal_paths
    review_loop_drift_recovery.restore_fatal_artifacts(
        request,
        monitoring_window,
        drift.fatal_paths,
    )
    assert target.is_file()
    assert target.read_bytes() == b"original result"


def test_reserved_directory_substitution_is_detected_and_restored(
    tmp_path: Path,
) -> None:
    request, output, _node_dir = make_drift_request(tmp_path)
    target = output.results_dir / "protected-directory"
    target.mkdir(parents=True)
    original_child = target / "original.md"
    original_child.write_bytes(b"original child")
    monitoring_window = review_loop_drift_capture.capture_drift_monitoring_window(
        node_id=request.node.id,
        node_dir=request.node_dir,
        output=output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
    )

    original_child.unlink()
    target.rmdir()
    target.mkdir()
    (target / "forged.md").write_text("forged", encoding="utf-8")

    drift = review_loop_drift_reserved.detect_shared_reserved_drift(
        request,
        monitoring_window,
    )

    assert target in drift.fatal_paths
    review_loop_drift_recovery.restore_fatal_artifacts(
        request,
        monitoring_window,
        drift.fatal_paths,
    )
    assert target.is_dir()
    assert original_child.read_bytes() == b"original child"
    assert not (target / "forged.md").exists()
