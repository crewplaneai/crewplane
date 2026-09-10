from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import crewplane.runtime.execution.workflow.node as workflow_node_module
import crewplane.runtime.execution.workflow.orchestration as workflow_module
import crewplane.runtime.execution.workflow.postconditions as workflow_postconditions_module
from crewplane.architecture.ports.artifacts import StageFinalizeResult
from crewplane.artifacts import OutputManager
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)
from crewplane.runtime.execution.workspace_files.generated import (
    GeneratedFileWorkspaceCleanupResult,
    GeneratedFileWorkspaceRegistry,
)
from crewplane.runtime.workspace.worktree.cache import (
    WorktreeReuseCleanupResult,
)
from tests.integration.runtime.execution.workflow.workflow_cleanup_support import (
    empty_cleanup_plan,
    single_node_cleanup_plan,
)


def _recovery_payload(
    registry: RuntimePublicationRegistry,
    path: Path,
) -> bytes | None:
    destination = BytesIO()
    if not registry.copy_recovery_payload_to(path, destination):
        return None
    return destination.getvalue()


def test_stage_publications_retain_all_recovery_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    result_file = tmp_path / "result.md"
    findings_file = tmp_path / "findings.md"
    generated_file = tmp_path / "generated.bin"
    node_state_file = tmp_path / "node-state.json"
    result_file.write_bytes(b"result")
    findings_file.write_bytes(b"findings")
    generated_file.write_bytes(b"generated")
    node_state_file.write_bytes(b"state")
    publications = RuntimePublicationRegistry()

    def do_nothing(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def return_node_state(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        return node_state_file

    def return_finalize_result(
        *args: object,
        **kwargs: object,
    ) -> StageFinalizeResult:
        del args, kwargs
        return StageFinalizeResult(
            stage_name="node",
            result_file=result_file,
            findings_file=findings_file,
            included_outputs=(),
            skipped_empty_outputs=(),
            warnings=(),
            generated_files=(generated_file,),
        )

    monkeypatch.setattr(
        workflow_node_module,
        "execute_input_stage",
        do_nothing,
    )
    monkeypatch.setattr(
        workflow_node_module,
        "emit_stage_finalize_logs",
        do_nothing,
    )
    monkeypatch.setattr(
        workflow_node_module,
        "write_successful_node_state",
        return_node_state,
    )
    monkeypatch.setattr(
        output,
        "finalize_node",
        return_finalize_result,
    )
    node = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(
            stage_path="input",
            output_path="input-result.md",
            log_path="input/logs",
            result_path="input-result.md",
        ),
    )
    runtime_context = SimpleNamespace(
        plan=single_node_cleanup_plan(output),
        generated_file_workspaces=GeneratedFileWorkspaceRegistry(),
        runtime_publications=publications,
    )

    asyncio.run(
        workflow_node_module.execute_node(
            node,
            output,
            invoker=object(),
            runtime_context=runtime_context,
            telemetry=None,
            workflow_identity="workflow",
        )
    )

    assert _recovery_payload(publications, result_file) == b"result"
    assert _recovery_payload(publications, findings_file) == b"findings"
    assert _recovery_payload(publications, node_state_file) == b"state"
    assert _recovery_payload(publications, generated_file) == b"generated"
    publications.close()


def test_workflow_closes_runtime_publication_registry_when_cleanup_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed_registries: list[RuntimePublicationRegistry] = []
    original_close = RuntimePublicationRegistry.close

    def record_close(registry: RuntimePublicationRegistry) -> None:
        original_close(registry)
        closed_registries.append(registry)

    def fail_cleanup(registry: GeneratedFileWorkspaceRegistry) -> None:
        del registry
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(RuntimePublicationRegistry, "close", record_close)
    monkeypatch.setattr(GeneratedFileWorkspaceRegistry, "cleanup_all", fail_cleanup)
    output = OutputManager("Workflow", base_dir=tmp_path)

    with pytest.raises(RuntimeError, match="cleanup exploded"):
        asyncio.run(
            workflow_module.execute_workflow(
                empty_cleanup_plan(output),
                output,
                invoker=object(),
                secret_context=SecretContext(),
                suppress_progress_output=True,
            )
        )

    assert len(closed_registries) == 1
    with pytest.raises(RuntimeError, match="registry is closed"):
        closed_registries[0].publish(
            tmp_path / "late.md", (0, hashlib.sha256().hexdigest())
        )


@pytest.mark.parametrize("deferred_failure", [False, True])
def test_workflow_postcondition_errors_preserve_phase_order_before_registry_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    deferred_failure: bool,
) -> None:
    calls: list[str] = []
    deferred_error = RuntimeError("deferred cleanup failed")
    ref_error = RuntimeError("ref cleanup failed")
    generated_error = RuntimeError("generated cleanup failed")
    worktree_error = RuntimeError("worktree cleanup failed")
    state_refresh_error = RuntimeError("state refresh failed")
    descriptor_refresh_error = RuntimeError("descriptor refresh failed")

    class DeferredWorkspaceCleanups:
        has_unfinished_protected_tasks = False

        async def drain(self, timeout_seconds: float) -> tuple[Exception, ...]:
            del timeout_seconds
            calls.append("deferred")
            return (deferred_error,) if deferred_failure else ()

    class GeneratedFileRegistry:
        def cleanup_all(self) -> GeneratedFileWorkspaceCleanupResult:
            calls.append("generated")
            return GeneratedFileWorkspaceCleanupResult((generated_error,), ("input",))

    class WorktreeReuseRegistry:
        def cleanup_all(self) -> WorktreeReuseCleanupResult:
            calls.append("worktree")
            return WorktreeReuseCleanupResult(
                (worktree_error,),
                (tmp_path / "workspace-state.json",),
            )

    class PublicationRegistry:
        def close(self) -> None:
            calls.append("close")

    async def fail_ref_cleanup(*args: object) -> None:
        del args
        calls.append("refs")
        raise ref_error

    async def fail_state_refresh(*args: object) -> tuple[tuple[str, Exception], ...]:
        del args
        calls.append("state-refresh")
        return (("input", state_refresh_error),)

    async def fail_descriptor_refresh(
        *args: object,
    ) -> tuple[tuple[str, Exception], ...]:
        del args
        calls.append("descriptor-refresh")
        return (("input", descriptor_refresh_error),)

    monkeypatch.setattr(
        workflow_postconditions_module,
        "cleanup_successful_workspace_run_refs",
        fail_ref_cleanup,
    )
    monkeypatch.setattr(
        workflow_postconditions_module,
        "refresh_workspace_node_manifests_for_state_paths",
        fail_state_refresh,
    )
    monkeypatch.setattr(
        workflow_postconditions_module,
        "refresh_workspace_node_manifests",
        fail_descriptor_refresh,
    )
    output = OutputManager("Workflow", base_dir=tmp_path)
    runtime_context = SimpleNamespace(
        plan=single_node_cleanup_plan(output),
        deferred_workspace_cleanups=DeferredWorkspaceCleanups(),
        generated_file_workspaces=GeneratedFileRegistry(),
        worktree_reuse_cache=WorktreeReuseRegistry(),
        runtime_publications=PublicationRegistry(),
    )
    session = SimpleNamespace(
        runtime_context=runtime_context,
        telemetry=None,
        state=SimpleNamespace(running={}, statuses={"input": "succeeded"}),
    )

    errors = asyncio.run(
        workflow_postconditions_module.collect_workflow_postconditions(
            session,
            output,
        )
    )

    if deferred_failure:
        assert errors == [
            deferred_error,
            ref_error,
            state_refresh_error,
            descriptor_refresh_error,
        ]
        assert calls == [
            "deferred",
            "refs",
            "state-refresh",
            "descriptor-refresh",
            "close",
        ]
    else:
        assert errors == [
            ref_error,
            generated_error,
            worktree_error,
            state_refresh_error,
            descriptor_refresh_error,
        ]
        assert calls == [
            "deferred",
            "refs",
            "generated",
            "worktree",
            "state-refresh",
            "descriptor-refresh",
            "close",
        ]
