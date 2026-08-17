import asyncio
import hashlib
import json
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import Event

import pytest

from crewplane.adapters.invokers.cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
)
from crewplane.architecture.contracts import InvocationProcessEvent, NodeArtifactRequest
from crewplane.architecture.ports import (
    ProviderProcessInvocation,
    ProviderProcessPublication,
)
from crewplane.artifacts import OutputManager
from crewplane.artifacts.generated_files.catalog import (
    generated_file_source_root,
)
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
)
from crewplane.core.preflight.runtime_config import (
    RuntimeAgentConfigSnapshot,
    runtime_agent_signature_payload,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.signatures import signature_for_payload
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    ExecutionEventContext,
    format_execution_event_log_line,
    invocation_event,
    runtime_log_event,
)
from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.agent.invoker import PlannedAgentInvoker
from crewplane.runtime.execution.common import (
    CompiledRuntimeContext,
    ProviderCallDisplay,
)
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)
from crewplane.runtime.execution.review_loop import (
    drift as review_loop_drift,
)
from crewplane.runtime.execution.review_loop import (
    drift_detection as review_loop_drift_detection,
)
from crewplane.runtime.execution.review_loop.types import (
    ActivityWindow,
    DriftGuardCallRequest,
    DriftMonitoringWindow,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    provider_failure,
)


def _recovery_payload(
    registry: RuntimePublicationRegistry,
    path: Path,
) -> bytes | None:
    destination = BytesIO()
    if not registry.copy_recovery_payload_to(path, destination):
        return None
    return destination.getvalue()


def _request(
    tmp_path: Path,
    use_cli_invoker: bool = False,
) -> tuple[DriftGuardCallRequest, OutputManager, Path]:
    output = OutputManager("workflow", base_dir=tmp_path)
    agent_config = AgentConfig(
        cli_cmd=(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdin.read(); print('provider output')",
            ]
            if use_cli_invoker
            else ["mock"]
        ),
        default_model=None if use_cli_invoker else "m1",
    )
    agent_payload = agent_config.model_dump(mode="json", exclude_none=True)
    invoker_alias = "cli" if use_cli_invoker else "mock"
    invoker_payload = {
        "capabilities": {},
        "implementation": invoker_alias,
        "options": {},
        "resolved_identity": invoker_alias,
    }
    agent_signature = _agent_signature("exec", agent_payload, None)
    node = PreflightExecutionNode(
        id="review.node",
        mode="sequential",
        render_plan_id="review.node",
        provider_records=[
            ProviderRecord(
                provider="exec",
                role=ProviderRole.EXECUTOR,
                task_id="exec_executor_0",
                agent_config_key="exec",
                invoker_alias=invoker_alias,
                agent_config_signature=agent_signature,
                invoker_config_signature=signature_for_payload(invoker_payload),
            )
        ],
        artifact_contract=ArtifactContract(
            stage_path="review.node",
            output_path="review.node-result.md",
            log_path="review.node/logs",
            result_path="review.node-result.md",
        ),
    )
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    request = DriftGuardCallRequest(
        runtime_context=CompiledRuntimeContext(
            plan=PreflightExecutionPlan(
                plan_schema_version=SCHEMA_VERSION,
                run_id="run-1",
                run_key_name="run-1",
                project_root=".",
                context_root=".",
                manifest_root=".crewplane",
                created_at="2026-06-03T00:00:00",
                workflow_name="workflow",
                workflow_signature="workflow-signature",
                execution_order=["review.node"],
                nodes=[node],
                render_plans=[
                    RenderPlan(
                        render_plan_id="review.node",
                        node_id="review.node",
                    )
                ],
                static_resources=[],
                token_catalog=[],
                dependency_graph=[],
                runtime_config_snapshot={
                    "agents": {"exec": agent_payload},
                    "execution": {},
                    "invoker": {**invoker_payload, "option_scopes": {}},
                    "schema_version": SCHEMA_VERSION,
                },
                effective_runtime_config_signature="runtime-signature",
                fingerprint_metadata={"payload_version": "1"},
            ),
            secret_context=SecretContext(),
        ),
        output=output,
        node=node,
        node_dir=node_dir,
        invoker=(
            PlannedAgentInvoker(
                plan_builder=build_cli_invocation_plan,
                log_presentation_builder=build_cli_log_presentation,
            )
            if use_cli_invoker
            else object()
        ),
        telemetry=None,
        audit_round_num=None,
        round_num=1,
        provider=ProviderRecord(
            provider="exec",
            role=ProviderRole.EXECUTOR,
            task_id="exec_executor_0",
            agent_config_key="exec",
            invoker_alias=invoker_alias,
            agent_config_signature=agent_signature,
            invoker_config_signature=signature_for_payload(invoker_payload),
        ),
        task_id="exec_executor_0",
        prompt="Prompt",
        output_file=node_dir / "exec_executor_0_round1.md",
        role_label=ProviderRole.EXECUTOR,
        findings_enabled=False,
        allowed_paths=set(),
        display=ProviderCallDisplay(
            telemetry=None,
            progress_description="Executing exec...",
        ),
    )
    return request, output, node_dir


def _agent_signature(
    agent_config_key: str,
    agent_payload: object,
    resolved_model: str | None,
) -> str:
    agent_snapshot = RuntimeAgentConfigSnapshot.model_validate(agent_payload)
    return signature_for_payload(
        runtime_agent_signature_payload(
            agent_config_key,
            agent_snapshot,
            resolved_model,
        )
    )


def test_current_invocation_and_parallel_reviewer_outputs_are_allowed(
    tmp_path: Path,
) -> None:
    _, output, node_dir = _request(tmp_path)
    executor_output = node_dir / "exec_executor_0_round1.md"
    reviewer_output = node_dir / "review_reviewer_0_round1.md"

    drift = review_loop_drift_detection.detect_artifact_drift(
        before_snapshot={},
        after_snapshot={executor_output: (1, "a"), reviewer_output: (1, "b")},
        allowed_paths={executor_output, reviewer_output},
        output=output,
        node_dir=node_dir,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_cli_provider_process_state_is_an_expected_runtime_publication(
    tmp_path: Path,
) -> None:
    request, output, _node_dir = _request(tmp_path, use_cli_invoker=True)
    request.allowed_paths.add(request.output_file)

    warning_count = asyncio.run(
        review_loop_drift.run_provider_call_with_drift_guard(request)
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
        _recovery_payload(
            request.runtime_publications,
            process_states[0],
        )
        == process_states[0].read_bytes()
    )
    request.runtime_publications.close()


def test_parallel_cli_provider_process_states_are_expected_publications(
    tmp_path: Path,
) -> None:
    first, output, node_dir = _request(tmp_path, use_cli_invoker=True)
    session = review_loop_drift.create_drift_guard_session(None)
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
            review_loop_drift.run_provider_call_with_drift_guard(first),
            review_loop_drift.run_provider_call_with_drift_guard(second),
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
    request, output, _node_dir = _request(tmp_path, use_cli_invoker=True)
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
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))


def test_preexisting_shared_reserved_drift_is_fatal_when_not_exclusive(
    tmp_path: Path,
) -> None:
    request, output, _ = _request(tmp_path)
    result_path = output.results_dir / "review.node-result.md"
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot={result_path: (1, "before")},
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=1),
    )

    drift = review_loop_drift_detection.detect_shared_reserved_drift(
        request,
        window,
        check_shared_reserved_drift=False,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == (result_path,)


def test_new_reserved_drift_is_fatal_when_not_exclusive(tmp_path: Path) -> None:
    request, output, _ = _request(tmp_path)
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

    drift = review_loop_drift_detection.detect_shared_reserved_drift(
        request,
        window,
        check_shared_reserved_drift=False,
    )

    assert drift.fatal_paths == (result_path,)


def test_monitoring_window_retries_concurrent_runtime_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, output, node_dir = _request(tmp_path)
    publications = request.runtime_context.runtime_publications
    result_path = output.results_dir / "peer-result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(b"original result")
    replacement_path = tmp_path / "peer-result-replacement.md"
    replacement_payload = b"published peer result"
    replacement_signature = (
        len(replacement_payload),
        hashlib.sha256(replacement_payload).hexdigest(),
    )
    original_read_bytes = Path.read_bytes
    publication_count = 0

    def publish_during_read(path: Path) -> bytes:
        nonlocal publication_count
        payload = original_read_bytes(path)
        if path == result_path and publication_count == 0:
            replacement_path.write_bytes(replacement_payload)
            with publications.transaction():
                os.replace(replacement_path, result_path)
                publications.publish(
                    result_path,
                    replacement_signature,
                    recovery_source=result_path,
                )
            publication_count += 1
        return payload

    monkeypatch.setattr(
        Path,
        "read_bytes",
        publish_during_read,
    )

    window = review_loop_drift_detection.capture_drift_monitoring_window(
        request.node.id,
        node_dir,
        output,
        telemetry=None,
        runtime_publications=publications,
    )

    assert publication_count == 1
    assert window.shared_reserved_original_bytes == {}
    assert window.shared_reserved_snapshot[result_path] == replacement_signature
    assert _recovery_payload(publications, result_path) == replacement_payload
    publications.close()


def test_monitoring_windows_share_disk_backed_recovery_baseline(
    tmp_path: Path,
) -> None:
    request, output, node_dir = _request(tmp_path)
    result_path = output.results_dir / "peer-result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(b"shared recovery payload")
    recovery_baseline = review_loop_drift_detection.capture_drift_recovery_baseline(
        node_dir,
        output,
        request.runtime_context.runtime_publications,
    )

    first = review_loop_drift_detection.capture_drift_monitoring_window(
        request.node.id,
        node_dir,
        output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
        recovery_baseline=recovery_baseline,
    )
    second = review_loop_drift_detection.capture_drift_monitoring_window(
        request.node.id,
        node_dir,
        output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
        recovery_baseline=recovery_baseline,
    )

    assert first.node_original_bytes is second.node_original_bytes
    assert first.shared_reserved_original_bytes is second.shared_reserved_original_bytes
    assert first.node_original_bytes == {}
    assert first.shared_reserved_original_bytes == {}
    assert (
        _recovery_payload(
            request.runtime_context.runtime_publications,
            result_path,
        )
        == b"shared recovery payload"
    )
    request.runtime_context.runtime_publications.close()


def test_recovery_baseline_without_registry_retains_original_bytes(
    tmp_path: Path,
) -> None:
    request, output, node_dir = _request(tmp_path)
    result_path = output.results_dir / "peer-result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(b"in-memory fallback")

    recovery_baseline = review_loop_drift_detection.capture_drift_recovery_baseline(
        node_dir,
        output,
    )

    assert recovery_baseline.shared_reserved_original_bytes == {
        result_path: b"in-memory fallback"
    }
    assert recovery_baseline.shared_reserved_snapshot[result_path] == (
        len(b"in-memory fallback"),
        hashlib.sha256(b"in-memory fallback").hexdigest(),
    )
    request.runtime_context.runtime_publications.close()


def test_registered_reserved_publication_created_during_window_is_restored(
    tmp_path: Path,
) -> None:
    request, output, node_dir = _request(tmp_path)
    publications = request.runtime_context.runtime_publications
    request.runtime_publications = publications
    window = review_loop_drift_detection.capture_drift_monitoring_window(
        request.node.id,
        node_dir,
        output,
        telemetry=None,
        runtime_publications=publications,
    )
    result_path = output.results_dir / "sibling-result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    trusted_payload = b"trusted sibling publication"
    result_path.write_bytes(trusted_payload)
    publications.publish(
        result_path,
        review_loop_drift_detection.file_snapshot_signature(result_path),
        recovery_source=result_path,
    )
    result_path.write_bytes(b"provider mutation")

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )
    review_loop_drift_detection.restore_fatal_artifacts(
        request,
        window,
        drift.fatal_paths,
    )

    assert result_path in drift.fatal_paths
    assert result_path.read_bytes() == trusted_payload
    publications.close()


@pytest.mark.parametrize(
    "target_kind",
    ["candidate", "peer_verdict", "result", "findings", "manifest"],
)
def test_drift_guard_rejects_and_restores_producer_owned_artifacts(
    tmp_path: Path,
    target_kind: str,
) -> None:
    request, output, node_dir = _request(tmp_path)
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
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

    assert target.read_bytes() == original


@pytest.mark.parametrize("target_kind", ["summary", "events"])
def test_drift_guard_restores_unsafe_strict_log_substitution(
    tmp_path: Path,
    target_kind: str,
) -> None:
    request, output, _node_dir = _request(tmp_path)
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
            asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

        assert not target.is_symlink()
        assert target.read_bytes() == original
    finally:
        request.runtime_context.runtime_publications.close()


def test_drift_guard_removes_forged_unpublished_peer_output(
    tmp_path: Path,
) -> None:
    request, _output, node_dir = _request(tmp_path)
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
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

    assert not peer_output.exists()


def test_nested_stage_drift_guard_rejects_and_restores_run_manifest(
    tmp_path: Path,
) -> None:
    request, output, _node_dir = _request(tmp_path)
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
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

    assert manifest_path.read_bytes() == original


def test_reserved_file_replaced_by_directory_is_restored(
    tmp_path: Path,
) -> None:
    request, output, _node_dir = _request(tmp_path)
    target = output.results_dir / "protected-result.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"original result")
    monitoring_window = review_loop_drift_detection.capture_drift_monitoring_window(
        node_id=request.node.id,
        node_dir=request.node_dir,
        output=output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
    )

    target.unlink()
    target.mkdir()
    (target / "forged.md").write_text("forged", encoding="utf-8")

    drift = review_loop_drift_detection.detect_shared_reserved_drift(
        request,
        monitoring_window,
        check_shared_reserved_drift=True,
    )

    assert target in drift.fatal_paths
    review_loop_drift_detection.restore_fatal_artifacts(
        request,
        monitoring_window,
        drift.fatal_paths,
    )
    assert target.is_file()
    assert target.read_bytes() == b"original result"


def test_reserved_directory_substitution_is_detected_and_restored(
    tmp_path: Path,
) -> None:
    request, output, _node_dir = _request(tmp_path)
    target = output.results_dir / "protected-directory"
    target.mkdir(parents=True)
    original_child = target / "original.md"
    original_child.write_bytes(b"original child")
    monitoring_window = review_loop_drift_detection.capture_drift_monitoring_window(
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

    drift = review_loop_drift_detection.detect_shared_reserved_drift(
        request,
        monitoring_window,
        check_shared_reserved_drift=True,
    )

    assert target in drift.fatal_paths
    review_loop_drift_detection.restore_fatal_artifacts(
        request,
        monitoring_window,
        drift.fatal_paths,
    )
    assert target.is_dir()
    assert original_child.read_bytes() == b"original child"
    assert not (target / "forged.md").exists()


@pytest.mark.parametrize("is_exclusive", [True, False])
def test_summary_drift_is_always_fatal(tmp_path: Path, is_exclusive: bool) -> None:
    request, output, _node_dir = _request(tmp_path)
    summary_path = output.get_run_summary_path()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_bytes(b"after")
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=b"before",
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=is_exclusive, version=1),
    )

    drift = review_loop_drift_detection.detect_summary_drift(request, window)

    assert drift.fatal_paths == (summary_path,)


@pytest.mark.parametrize("strict_expected_append", [True, False])
def test_event_log_destructive_drift_is_always_fatal(
    tmp_path: Path,
    strict_expected_append: bool,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=b"before\n",
        after=b"truncated",
        expected_append=b"",
        strict_expected_append=strict_expected_append,
    )

    assert drift.fatal_paths == (event_log_path,)


def test_event_log_restoration_preserves_registered_concurrent_appends(
    tmp_path: Path,
) -> None:
    request, output, node_dir = _request(tmp_path)
    publications = request.runtime_context.runtime_publications
    request.runtime_publications = publications
    event_log_path = output.get_run_event_log_path()
    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    baseline = b"baseline\n"
    concurrent_append = b"concurrent runtime event\n"
    event_log_path.write_bytes(baseline)
    window = review_loop_drift_detection.capture_drift_monitoring_window(
        request.node.id,
        node_dir,
        output,
        telemetry=None,
        runtime_publications=publications,
    )
    with publications.event_publication("sibling", concurrent_append):
        event_log_path.write_bytes(baseline + concurrent_append)
    event_log_path.write_bytes(b"provider mutation")

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )
    review_loop_drift_detection.restore_fatal_artifacts(
        request,
        window,
        drift.fatal_paths,
    )

    assert event_log_path in drift.fatal_paths
    assert event_log_path.read_bytes() == baseline + concurrent_append


def test_event_log_absent_before_after_no_expected_append_is_not_fatal(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=None,
        after=None,
        expected_append=b"",
        strict_expected_append=True,
    )

    assert drift.fatal_paths == ()
    assert drift.warning_paths == ()


def test_event_log_absent_before_after_expected_append_is_not_fatal(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=None,
        after=None,
        expected_append=b"unexpected event\n",
        strict_expected_append=True,
    )

    assert drift.fatal_paths == ()
    assert drift.warning_paths == ()


def test_event_log_absent_before_after_expected_append_must_match(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"
    expected_append = b"appended\\n"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=None,
        after=expected_append,
        expected_append=expected_append,
        strict_expected_append=True,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_event_log_empty_creation_is_fatal_under_strict_append_check(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=None,
        after=b"",
        expected_append=b"",
        strict_expected_append=True,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == (event_log_path,)


def test_event_log_creation_mismatch_is_ignored_when_not_strict(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=None,
        after=b"concurrent event\n",
        expected_append=b"expected event\n",
        strict_expected_append=False,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_event_log_append_mismatch_is_fatal_only_under_strict_append_check(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    strict_drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=b"before\n",
        after=b"before\nunexpected\n",
        expected_append=b"expected\n",
        strict_expected_append=True,
    )
    non_strict_drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=b"before\n",
        after=b"before\nunexpected\n",
        expected_append=b"expected\n",
        strict_expected_append=False,
    )

    assert strict_drift.fatal_paths == (event_log_path,)
    assert non_strict_drift.fatal_paths == ()


def test_registered_event_append_requires_this_invocation_attribution(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"
    expected_append = b"expected provider event\n"
    concurrent_append = b"concurrent provider event\n"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=b"baseline\n",
        after=b"baseline\n" + concurrent_append,
        expected_append=expected_append,
        strict_expected_append=True,
        registered_append=concurrent_append,
        registered_owned_append=b"",
    )

    assert drift.fatal_paths == (event_log_path,)


def test_event_log_append_allows_ambient_runtime_warning(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"
    started = format_execution_event_log_line(
        invocation_event(
            "invocation_started",
            "workflow",
            "run-1",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="review.node",
                provider="exec",
                role=ProviderRole.EXECUTOR,
                task_id="exec_executor_0",
            ),
        )
    ).encode("utf-8")
    finished = format_execution_event_log_line(
        invocation_event(
            "invocation_finished",
            "workflow",
            "run-1",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="review.node",
                provider="exec",
                role=ProviderRole.EXECUTOR,
                task_id="exec_executor_0",
            ),
        )
    ).encode("utf-8")
    ambient_warning = format_execution_event_log_line(
        runtime_log_event(
            "workflow",
            "run-1",
            level="warning",
            message="tmux command timed out; live dashboard may be stale",
            operation="runtime_warning",
        )
    ).encode("utf-8")

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=b'{"event":"baseline"}\n',
        after=b'{"event":"baseline"}\n' + started + ambient_warning + finished,
        expected_append=started + finished,
        strict_expected_append=True,
    )

    assert drift.fatal_paths == ()
    assert drift.warning_paths == ()


def test_node_local_unexpected_writes_are_warning_level(tmp_path: Path) -> None:
    _, output, node_dir = _request(tmp_path)
    unexpected = node_dir / "review-state" / "mutated-note.md"

    drift = review_loop_drift_detection.detect_artifact_drift(
        before_snapshot={},
        after_snapshot={unexpected: (1, "hash")},
        allowed_paths=set(),
        output=output,
        node_dir=node_dir,
    )

    assert drift.warning_paths == (unexpected,)
    assert drift.fatal_paths == ()


def test_node_local_child_write_preserves_existing_directory_state(
    tmp_path: Path,
) -> None:
    request, _output, node_dir = _request(tmp_path)
    review_state = node_dir / "review-state"
    review_state.mkdir()
    prior_state = review_state / "prior.state.json"
    prior_state.write_text('{"round": 0}\n', encoding="utf-8")
    provider_note = review_state / "provider-note.md"

    class NodeLocalWritingInvoker:
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
            provider_note.write_text("provider note\n", encoding="utf-8")
            output_file.write_text("provider output\n", encoding="utf-8")

    request.invoker = NodeLocalWritingInvoker()

    warning_count = asyncio.run(
        review_loop_drift.run_provider_call_with_drift_guard(request)
    )

    assert warning_count == 1
    assert prior_state.read_text(encoding="utf-8") == '{"round": 0}\n'
    assert provider_note.read_text(encoding="utf-8") == "provider note\n"


def test_fatal_node_root_drift_is_restored(tmp_path: Path) -> None:
    request, _output, node_dir = _request(tmp_path)
    original_mode = stat.S_IMODE(node_dir.stat().st_mode)
    request.invocation_output_file = tmp_path / "provider-output.md"
    request.defer_output_publication = True

    class RootMetadataMutatingInvoker:
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
            output_file.write_text("provider output\n", encoding="utf-8")
            node_dir.chmod(0o555)

    request.invoker = RootMetadataMutatingInvoker()

    try:
        with pytest.raises(NodeExecutionError, match="modified fatal artifacts"):
            asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))
        assert stat.S_IMODE(node_dir.stat().st_mode) == original_mode
    finally:
        node_dir.chmod(original_mode)


def test_allowed_output_parent_replacement_is_fatal(tmp_path: Path) -> None:
    request, _output, node_dir = _request(tmp_path)
    audit_dir = node_dir / "review-audit-round-1"
    audit_dir.mkdir()
    request.output_file = audit_dir / "exec_executor_0_round1.md"
    request.allowed_paths = {request.output_file}
    displaced_dir = tmp_path / "displaced-audit-round-1"

    class OutputParentReplacingInvoker:
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
            audit_dir.rename(displaced_dir)
            audit_dir.mkdir()
            output_file.write_text("provider output\n", encoding="utf-8")

    request.invoker = OutputParentReplacingInvoker()

    with pytest.raises(NodeExecutionError, match="modified fatal artifacts"):
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))


def test_fatal_node_directory_drift_restores_unchanged_descendants(
    tmp_path: Path,
) -> None:
    request, _output, node_dir = _request(tmp_path)
    review_state = node_dir / "review-state"
    review_state.mkdir()
    original_mode = stat.S_IMODE(review_state.stat().st_mode)
    prior_state = review_state / "prior.state.json"
    prior_state.write_text('{"round": 0}\n', encoding="utf-8")
    untouched_script = review_state / "untouched.sh"
    untouched_script.write_text("#!/bin/sh\n", encoding="utf-8")
    untouched_script.chmod(0o754)
    untouched_link = review_state / "untouched-link"
    untouched_link.symlink_to(untouched_script.name)

    class DirectoryMetadataMutatingInvoker:
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
            review_state.chmod(original_mode ^ stat.S_IWGRP)
            output_file.write_text("provider output\n", encoding="utf-8")

    request.invoker = DirectoryMetadataMutatingInvoker()

    with pytest.raises(NodeExecutionError, match="modified fatal artifacts"):
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

    assert stat.S_IMODE(review_state.stat().st_mode) == original_mode
    assert prior_state.read_text(encoding="utf-8") == '{"round": 0}\n'
    assert stat.S_IMODE(untouched_script.stat().st_mode) == 0o754
    assert untouched_link.is_symlink()
    assert untouched_link.readlink() == Path(untouched_script.name)


def test_directory_restore_restores_group_before_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, node_dir = _request(tmp_path)
    target = node_dir / "review-state"
    target.mkdir()
    monitoring_window = review_loop_drift_detection.capture_drift_monitoring_window(
        node_id=request.node.id,
        node_dir=node_dir,
        output=request.output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
    )
    expected = monitoring_window.node_directory_snapshot[target]
    expected = replace(
        expected,
        group_id=expected.group_id + 1,
        mode=expected.mode ^ stat.S_IWGRP,
    )
    monitoring_window.node_directory_snapshot[target] = expected
    calls: list[tuple[str, int, int | None]] = []

    def record_chown(path: Path, user_id: int, group_id: int) -> None:
        assert path == target
        calls.append(("chown", user_id, group_id))

    def record_chmod(path: Path, mode: int) -> None:
        assert path == target
        calls.append(("chmod", mode, None))

    monkeypatch.setattr(os, "chown", record_chown)
    monkeypatch.setattr(Path, "chmod", record_chmod)

    review_loop_drift_detection.restore_fatal_artifacts(
        request,
        monitoring_window,
        (target,),
    )

    assert calls == [
        ("chown", expected.user_id, expected.group_id),
        ("chmod", stat.S_IMODE(expected.mode), None),
    ]


def test_drift_snapshots_record_unsafe_entries_without_reading_targets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    symlink = root / "linked.txt"
    hardlink = root / "hardlinked.txt"
    try:
        symlink.symlink_to(outside)
        os.link(outside, hardlink)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"link creation is unavailable: {exc}")

    snapshot = review_loop_drift_detection.snapshot_files(root)

    assert symlink in snapshot
    assert hardlink in snapshot
    assert review_loop_drift_detection.snapshot_file_bytes(root) == {}


def test_drift_snapshots_reject_a_symlinked_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        review_loop_drift_detection.snapshot_files(linked_root)


def test_drift_snapshot_ignores_a_file_that_disappears_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    transient = root / ".runtime-publication.tmp"
    transient.write_text("publishing", encoding="utf-8")
    original_signature = review_loop_drift_detection.file_snapshot_signature

    def remove_transient_before_read(path: Path) -> tuple[int, str]:
        if path == transient:
            path.unlink()
        return original_signature(path)

    monkeypatch.setattr(
        review_loop_drift_detection,
        "file_snapshot_signature",
        remove_transient_before_read,
    )

    assert review_loop_drift_detection.snapshot_files(root) == {}


def test_runtime_generated_file_source_snapshots_are_allowed(
    tmp_path: Path,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
    snapshot_root = generated_file_source_root(request.output_file)
    generated_source = snapshot_root / "src/app.txt"
    generated_source.parent.mkdir(parents=True)
    generated_source.write_text("generated", encoding="utf-8")
    allowance = review_loop_drift.GeneratedFileDriftAllowance()
    allowance.start_snapshot(snapshot_root)
    allowance.finish_snapshot(
        snapshot_root,
        {
            generated_source: (
                review_loop_drift_detection.file_snapshot_signature(generated_source)
            )
        },
    )
    request.generated_file_allowance = allowance
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_unregistered_generated_file_source_snapshots_are_drift(
    tmp_path: Path,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
    generated_source = generated_file_source_root(request.output_file) / "src/app.txt"
    generated_source.parent.mkdir(parents=True)
    generated_source.write_text("generated", encoding="utf-8")
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )

    assert generated_source not in request.allowed_paths
    assert drift.warning_paths == (generated_source,)
    assert drift.fatal_paths == ()


def test_completed_generated_file_snapshot_allows_only_published_files(
    tmp_path: Path,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
    request.drift_session = review_loop_drift.create_drift_guard_session(None)
    snapshot_root = generated_file_source_root(request.output_file)
    published_source = snapshot_root / "src/app.txt"
    allowance = request.drift_session.generated_file_allowance
    allowance.start_snapshot(snapshot_root)
    published_source.parent.mkdir(parents=True)
    published_source.write_text("generated", encoding="utf-8")
    allowance.finish_snapshot(
        snapshot_root,
        {
            published_source: (
                review_loop_drift_detection.file_snapshot_signature(published_source)
            )
        },
    )

    unexpected_source = snapshot_root / "src/unregistered.txt"
    unexpected_source.write_text("unexpected", encoding="utf-8")
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )

    assert drift.warning_paths == (unexpected_source,)
    assert drift.fatal_paths == ()


@pytest.mark.parametrize(
    ("relative_path", "mutation"),
    [
        ("generated-files.json", "rewrite"),
        ("generated-files.json", "delete"),
        ("src/app.txt", "rewrite"),
        ("src/app.txt", "delete"),
    ],
)
def test_completed_generated_file_snapshot_detects_publication_drift(
    tmp_path: Path,
    relative_path: str,
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
    request.drift_session = review_loop_drift.create_drift_guard_session(None)
    snapshot_root = generated_file_source_root(request.output_file)
    published_path = snapshot_root / relative_path
    published_path.parent.mkdir(parents=True, exist_ok=True)
    published_path.write_text("runtime publication", encoding="utf-8")
    allowance = request.drift_session.generated_file_allowance
    allowance.start_snapshot(snapshot_root)
    allowance.finish_snapshot(
        snapshot_root,
        {
            published_path: (
                review_loop_drift_detection.file_snapshot_signature(published_path)
            )
        },
    )
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )
    scan_started = Event()
    mutation_complete = Event()
    original_snapshot_files = review_loop_drift_detection.snapshot_files

    def snapshot_after_concurrent_mutation(
        root: Path,
        excluded_paths: set[Path] | None = None,
        excluded_roots: set[Path] | None = None,
    ) -> dict[Path, tuple[int, str]]:
        if root == request.node_dir:
            scan_started.set()
            assert mutation_complete.wait(timeout=5)
        return original_snapshot_files(root, excluded_paths, excluded_roots)

    monkeypatch.setattr(
        review_loop_drift_detection,
        "snapshot_files",
        snapshot_after_concurrent_mutation,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        drift_future = executor.submit(
            review_loop_drift_detection.detect_provider_call_drift,
            request,
            window,
            None,
            0,
        )
        assert scan_started.wait(timeout=5)
        if mutation == "rewrite":
            published_path.write_text("other reviewer", encoding="utf-8")
        else:
            published_path.unlink()
        mutation_complete.set()
        drift = drift_future.result(timeout=5)

    assert drift.warning_paths == (published_path,)
    assert drift.fatal_paths == ()


def test_generated_file_snapshot_published_during_node_scan_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
    request.drift_session = review_loop_drift.create_drift_guard_session(None)
    snapshot_root = generated_file_source_root(request.output_file)
    generated_source = snapshot_root / "src/app.txt"
    unexpected_source = snapshot_root / "src/unregistered.txt"
    allowance = request.drift_session.generated_file_allowance
    original_snapshot_files = review_loop_drift_detection.snapshot_files

    published = False
    node_scan_count = 0

    def publish_generated_file_after_scan(
        root: Path,
        excluded_paths: set[Path] | None = None,
        excluded_roots: set[Path] | None = None,
    ) -> dict[Path, tuple[int, str]]:
        nonlocal node_scan_count, published
        snapshot = original_snapshot_files(root, excluded_paths, excluded_roots)
        if root != request.node_dir or published:
            if root == request.node_dir:
                node_scan_count += 1
            return snapshot
        node_scan_count += 1
        published = True
        allowance.start_snapshot(snapshot_root)
        generated_source.parent.mkdir(parents=True)
        generated_source.write_text("generated", encoding="utf-8")
        unexpected_source.write_text("unexpected", encoding="utf-8")
        allowance.finish_snapshot(
            snapshot_root,
            {
                generated_source: (
                    review_loop_drift_detection.file_snapshot_signature(
                        generated_source
                    )
                )
            },
        )
        return snapshot

    monkeypatch.setattr(
        review_loop_drift_detection,
        "snapshot_files",
        publish_generated_file_after_scan,
    )
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_node_drift(request, window)

    assert drift.warning_paths == (unexpected_source,)
    assert drift.fatal_paths == ()
    assert node_scan_count == 2


def test_in_progress_generated_file_root_is_not_read_during_node_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
    request.drift_session = review_loop_drift.create_drift_guard_session(None)
    snapshot_root = generated_file_source_root(request.output_file)
    changing_file = snapshot_root / "src/changing.bin"
    changing_file.parent.mkdir(parents=True)
    changing_file.write_bytes(b"runtime write")
    allowance = request.drift_session.generated_file_allowance
    allowance.start_snapshot(snapshot_root)
    allowance.finish_snapshot(
        snapshot_root,
        {
            changing_file: (
                review_loop_drift_detection.file_snapshot_signature(changing_file)
            )
        },
    )
    allowance.start_snapshot(snapshot_root)
    original_read_bytes = Path.read_bytes
    read_count = 0

    def append_after_each_read(path: Path) -> bytes:
        nonlocal read_count
        payload = original_read_bytes(path)
        if path == changing_file:
            read_count += 1
            with path.open("ab") as stream:
                stream.write(b"x")
        return payload

    monkeypatch.setattr(Path, "read_bytes", append_after_each_read)
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_node_drift(request, window)

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()
    assert read_count == 0


def test_peer_runtime_owned_file_does_not_create_directory_drift(
    tmp_path: Path,
) -> None:
    request, _output, node_dir = _request(tmp_path)
    peer_state = node_dir / "workspace-state" / "peer.json"
    request.runtime_owned_paths.add(peer_state)
    window = review_loop_drift_detection.capture_drift_monitoring_window(
        node_id=request.node.id,
        node_dir=node_dir,
        output=request.output,
        telemetry=None,
        runtime_publications=request.runtime_context.runtime_publications,
        runtime_owned_paths=request.runtime_owned_paths,
    )

    peer_state.parent.mkdir()
    peer_state.write_text("runtime state", encoding="utf-8")

    drift = review_loop_drift_detection.detect_node_drift(request, window)

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_failed_generated_file_snapshot_does_not_allow_partial_files(
    tmp_path: Path,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
    request.drift_session = review_loop_drift.create_drift_guard_session(None)
    snapshot_root = generated_file_source_root(request.output_file)
    partial_source = snapshot_root / "src/partial.txt"
    allowance = request.drift_session.generated_file_allowance
    allowance.start_snapshot(snapshot_root)
    partial_source.parent.mkdir(parents=True)
    partial_source.write_text("partial", encoding="utf-8")
    allowance.finish_snapshot(snapshot_root, None)
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )

    assert drift.warning_paths == (partial_source,)
    assert drift.fatal_paths == ()


def test_drift_is_checked_when_provider_call_fails(tmp_path: Path) -> None:
    request, _output, node_dir = _request(tmp_path)

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
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("artifact drift detected" in note for note in notes)


def test_fatal_drift_after_provider_call_failure_raises_fatal_error(
    tmp_path: Path,
) -> None:
    request, output, _node_dir = _request(tmp_path)

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
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

    assert isinstance(exc_info.value.__cause__, InvocationFailureError)
    assert "provider boom" in str(exc_info.value.__cause__)
    notes = getattr(exc_info.value.__cause__, "__notes__", [])
    assert any("1 fatal path" in note for note in notes)


def test_drift_detection_defect_after_expected_provider_failure_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = _request(tmp_path)

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

    def broken_drift_detection(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise TypeError("simulated drift detector defect")

    monkeypatch.setattr(
        review_loop_drift,
        "detect_provider_call_drift",
        broken_drift_detection,
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
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

    assert type(exc_info.value) is TypeError
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("expected provider failure" in note for note in notes)


def test_drift_detection_defect_preserves_unexpected_provider_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
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

    def broken_drift_detection(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise RuntimeError("simulated drift detector defect")

    monkeypatch.setattr(
        review_loop_drift,
        "detect_provider_call_drift",
        broken_drift_detection,
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
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

    assert exc_info.value.__cause__ is original_cause
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("artifact drift detection failed" in note for note in notes)


def test_drift_detection_defect_preserves_unexpected_provider_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
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

    def broken_drift_detection(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise RuntimeError("simulated drift detector defect")

    monkeypatch.setattr(
        review_loop_drift,
        "detect_provider_call_drift",
        broken_drift_detection,
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
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

    assert exc_info.value.__context__ is original_context
    assert exc_info.value.__suppress_context__ is False
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("artifact drift detection failed" in note for note in notes)
