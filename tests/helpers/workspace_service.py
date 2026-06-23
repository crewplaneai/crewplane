from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from crewplane.architecture.contracts import InvocationContext
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.manager import OutputManager
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.core.preflight.secrets import FINGERPRINT_PAYLOAD_VERSION
from crewplane.core.preflight.signatures import signature_for_payload
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.policy import WorktreeContract
from crewplane.runtime.workspace import WorkspaceInvocationRequest
from crewplane.runtime.workspace.service import MaterializationLimiter
from crewplane.version import SCHEMA_VERSION


def create_git_repo(tmp_path: Path, object_format: str = "sha1") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_args = (
        ("init",)
        if object_format == "sha1"
        else ("init", f"--object-format={object_format}")
    )
    run_git_text(repo, *init_args)
    run_git_text(repo, "config", "user.name", "Crewplane Test")
    run_git_text(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(repo, "add", "README.md")
    run_git_text(repo, "commit", "-m", "initial")
    return repo


def run_git_text(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode().strip()


def git_commit_exists(repo: Path, object_id: str) -> bool:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), "cat-file", "-e", f"{object_id}^{{commit}}"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workspace_plan(
    repo: Path,
    cache_root: Path,
    cleanup_on_success: bool,
    launch_mode: str = "runtime_command_runner",
    controlled_child_environment: bool = True,
    kind: str = "snapshot",
) -> PreflightExecutionPlan:
    runtime_snapshot = _runtime_snapshot(
        cache_root,
        cleanup_on_success,
        launch_mode=launch_mode,
        controlled_child_environment=controlled_child_environment,
    )
    return PreflightExecutionPlan(
        run_id="run-001",
        run_key_name="workspace-run-001",
        project_root=repo.as_posix(),
        context_root=repo.as_posix(),
        manifest_root=(repo / ".crewplane").as_posix(),
        created_at=datetime.now(UTC).isoformat(),
        workflow_name="workspace",
        workflow_signature="workflow-signature",
        execution_order=["implement"],
        nodes=[
            _node(
                workspace_enabled=True,
                runtime_snapshot=runtime_snapshot,
                kind=kind,
            )
        ],
        render_plans=[],
        static_resources=[],
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot=runtime_snapshot,
        effective_runtime_config_signature="runtime-signature",
        workspace_source=_workspace_source(repo),
        fingerprint_metadata={"payload_version": FINGERPRINT_PAYLOAD_VERSION},
    )


def disabled_workspace_plan(repo: Path) -> PreflightExecutionPlan:
    runtime_snapshot = _runtime_snapshot(
        None,
        True,
        launch_mode="runtime_command_runner",
        controlled_child_environment=True,
    )
    return PreflightExecutionPlan(
        run_id="run-001",
        run_key_name="workspace-run-001",
        project_root=repo.as_posix(),
        context_root=repo.as_posix(),
        manifest_root=(repo / ".crewplane").as_posix(),
        created_at=datetime.now(UTC).isoformat(),
        workflow_name="workspace",
        workflow_signature="workflow-signature",
        execution_order=["implement"],
        nodes=[
            _node(
                workspace_enabled=False,
                runtime_snapshot=runtime_snapshot,
                kind="snapshot",
            )
        ],
        render_plans=[],
        static_resources=[],
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot=runtime_snapshot,
        effective_runtime_config_signature="runtime-signature",
        fingerprint_metadata={"payload_version": FINGERPRINT_PAYLOAD_VERSION},
    )


def _node(
    workspace_enabled: bool,
    runtime_snapshot: dict[str, object],
    kind: str,
) -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="implement",
        mode="sequential",
        render_plan_id="implement",
        provider_records=[
            ProviderRecord(
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha",
                agent_config_key="alpha",
                invoker_alias="mock",
                agent_config_signature=_agent_signature(runtime_snapshot),
                invoker_config_signature=_invoker_signature(runtime_snapshot),
            )
        ],
        workspace_policy=(
            WorkspaceSelectionRecord(
                enabled=True,
                logical_worktree_name="primary",
                declaration_kind=kind,
                source_kind="project",
                source_node_id=None,
                clean_start="strict",
                materialization=_materialization(kind),
                worktree_contract=WorktreeContract(),
                writable=True,
                lineage_producer=kind == "worktree",
            )
            if workspace_enabled
            else None
        ),
        artifact_contract=ArtifactContract(
            stage_path="implement",
            output_path="implement/output.md",
        ),
    )


def _workspace_source(repo: Path) -> WorkspaceSourceSnapshot:
    git_dir = repo / ".git"
    return WorkspaceSourceSnapshot(
        worktree_contract=WorktreeContract(),
        run_base_commit=run_git_text(repo, "rev-parse", "HEAD^{commit}"),
        source_tree=run_git_text(repo, "rev-parse", "HEAD^{tree}"),
        object_format=run_git_text(repo, "rev-parse", "--show-object-format=storage"),
        repository_id="test-repo",
        git_version=run_git_text(repo, "--version"),
        git_top_level=repo.as_posix(),
        project_root_relative_path=".",
        active_git_dir=git_dir.as_posix(),
        common_git_dir=git_dir.as_posix(),
        clean_start="strict",
    )


def _runtime_snapshot(
    cache_root: Path | None,
    cleanup_on_success: bool,
    launch_mode: str,
    controlled_child_environment: bool,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "agents": {
            "alpha": {
                "cli_cmd": [sys.executable],
                "provider_kind": "generic",
                "model_arg": "--model",
                "prompt_transport": "stdin",
                "extra_args": [],
                "max_retries": 0,
                "retry_delay_seconds": 300.0,
                "retry_on_exit_codes": [],
                "retry_on_stderr_contains": [],
                "retry_on_output_contains": [],
                "quota_reached_on_contains": [],
                "quota_reached_retry_delay_seconds": 300.0,
                "quota_reset_sleep_floor_seconds": 5.0,
                "invocation_idle_timeout_seconds": 1800.0,
                "pricing": {
                    "input": None,
                    "cached_input": None,
                    "cache_write": None,
                    "output": None,
                    "reasoning": None,
                    "total": None,
                },
            }
        },
        "workspace": {
            "enabled": cache_root is not None,
            "cache_root": cache_root.as_posix() if cache_root is not None else None,
            "cleanup_on_success": cleanup_on_success,
            "worktree_contract": {
                "mode": "blob_exact",
                "schema_version": SCHEMA_VERSION,
            },
            "clean_start": "strict",
            "setup_profiles": {},
            "setup_timeout_seconds": 600.0,
            "identity": {"include_cache_root": False},
            "max_concurrent_materializations": 1,
            "disk": {},
        },
        "invoker": {
            "implementation": "mock",
            "resolved_identity": "crewplane.adapters.invokers.mock:MockInvokerAdapter",
            "options": {},
            "option_scopes": {},
            "capabilities": {
                "workspace": {
                    "supported": True,
                    "launch_mode": launch_mode,
                    "honors_cwd": True,
                    "controlled_child_environment": controlled_child_environment,
                }
            },
        },
        "artifacts": {
            "implementation": "filesystem",
            "resolved_identity": "crewplane.adapters.artifacts.filesystem:FilesystemArtifactAdapter",
            "options": {},
            "option_scopes": {},
            "capabilities": {},
        },
        "ui": {
            "implementation": "none",
            "resolved_identity": "crewplane.adapters.ui.null:NullUIAdapter",
            "options": {},
            "option_scopes": {},
            "capabilities": {},
        },
    }


def _agent_signature(runtime_snapshot: dict[str, object]) -> str:
    agents = runtime_snapshot["agents"]
    assert isinstance(agents, dict)
    return signature_for_payload(
        {
            "agent_config": agents["alpha"],
            "agent_config_key": "alpha",
        }
    )


def _invoker_signature(runtime_snapshot: dict[str, object]) -> str:
    invoker = runtime_snapshot["invoker"]
    assert isinstance(invoker, dict)
    return signature_for_payload(
        {
            "capabilities": invoker["capabilities"],
            "implementation": invoker["implementation"],
            "options": {},
            "resolved_identity": invoker["resolved_identity"],
        }
    )


def _materialization(kind: str) -> str:
    return "worktree_checkout" if kind == "worktree" else "snapshot_checkout"


def workspace_output_manager(
    tmp_path: Path,
    repo: Path,
    log_cli_output: bool = False,
) -> OutputManager:
    return OutputManager(
        "workspace",
        base_dir=tmp_path / ".crewplane",
        template_base_dir=repo,
        log_cli_output=log_cli_output,
    )


def workspace_invocation_request(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    role_label: ProviderRole = ProviderRole.EXECUTOR,
    audit_round_num: int | None = None,
    materialization_limiter: MaterializationLimiter | None = None,
) -> WorkspaceInvocationRequest:
    return WorkspaceInvocationRequest(
        plan=plan,
        output=output,
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role_label=role_label,
        round_num=1,
        audit_round_num=audit_round_num,
        materialization_limiter=materialization_limiter
        or MaterializationLimiter.from_plan(plan),
    )


def workspace_invocation_context(
    role: ProviderRole = ProviderRole.EXECUTOR,
) -> InvocationContext:
    return InvocationContext(
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role=role,
        audit_round_num=None,
        round_num=1,
        findings_enabled=False,
    )


def read_json_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload
