from __future__ import annotations

from orchestrator_cli.core.file_hashing import file_size_and_sha256
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from orchestrator_cli.core.preflight.workspace.observability import (
    invoker_workspace_descriptor,
)
from orchestrator_cli.core.value_checks import is_strict_int
from orchestrator_cli.version import SCHEMA_VERSION

from ...run_history import RunHistoryRecord
from ...safe_files import contained_regular_file
from ..bundle_validation import workspace_bundle_contains_result_tree
from ..rendered_file_validation import (
    provider_rendered_workspace_files_match,
)
from ..source_validation import workspace_invocation_source_matches
from .expected_set import workspace_state_payloads_match_expected_set
from .fields import (
    bool_field_matches as _bool_field_matches,
)
from .fields import (
    is_hex_object as _is_hex_object,
)
from .fields import (
    mapping_value as _mapping,
)
from .invocations import (
    ExpectedWorkspaceInvocation,
    expected_failed_workspace_invocations,
    expected_seeded_lineage_invocation,
    expected_workspace_invocations,
    failed_workspace_state_payloads,
    latest_lineage_payload_before,
    payload_matches_expected_invocation,
    workspace_state_payloads,
)


def workspace_node_state_is_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
) -> bool:
    policy = node.workspace_policy
    if policy is None or not policy.enabled:
        return True
    expected_invocations = expected_workspace_invocations(source, node)
    expected_failed_invocations = expected_failed_workspace_invocations(source, node)
    if not expected_invocations and not expected_failed_invocations:
        return False
    if not _parallel_workspace_outputs_are_complete(
        node,
        expected_invocations,
        expected_failed_invocations,
    ):
        return False
    if expected_failed_invocations and _lineage_worktree_node(node):
        return False
    state_payloads = workspace_state_payloads(source, node)
    if expected_invocations and not state_payloads:
        return False
    if not expected_invocations and state_payloads:
        return False
    if state_payloads and not all(
        _provider_workspace_state_is_valid(source, plan, node, payload)
        for payload in state_payloads
    ):
        return False
    if state_payloads and not workspace_state_payloads_match_expected_set(
        state_payloads,
        expected_invocations,
    ):
        return False
    if not all(
        _expected_workspace_invocation_is_valid(
            source,
            plan,
            node,
            state_payloads,
            expected,
        )
        for expected in expected_invocations
    ):
        return False
    return _failed_workspace_invocations_are_valid(
        source,
        plan,
        node,
        expected_failed_invocations,
    )


def _parallel_workspace_outputs_are_complete(
    node: PreflightExecutionNode,
    expected_invocations: tuple[ExpectedWorkspaceInvocation, ...],
    expected_failed_invocations: tuple[ExpectedWorkspaceInvocation, ...],
) -> bool:
    if node.mode != "parallel":
        return True
    expected_count = sum(
        1 for provider in node.provider_records if provider.role == "executor"
    )
    actual_count = len(expected_invocations) + len(expected_failed_invocations)
    return actual_count == expected_count


def _lineage_worktree_node(node: PreflightExecutionNode) -> bool:
    policy = node.workspace_policy
    return (
        policy is not None
        and policy.enabled
        and policy.materialization == "worktree_checkout"
        and policy.lineage_producer
    )


def _expected_workspace_invocation_is_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payloads: tuple[dict[str, object], ...],
    expected: ExpectedWorkspaceInvocation,
) -> bool:
    matches = [
        payload
        for payload in payloads
        if payload_matches_expected_invocation(payload, expected)
    ]
    if len(matches) == 1:
        return _provider_workspace_state_is_valid(source, plan, node, matches[0])
    if matches:
        return False
    if not expected_seeded_lineage_invocation(expected):
        return False
    seeded_source = latest_lineage_payload_before(payloads, expected)
    return seeded_source is not None and _provider_workspace_state_is_valid(
        source,
        plan,
        node,
        seeded_source,
    )


def _failed_workspace_invocations_are_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    expected_invocations: tuple[ExpectedWorkspaceInvocation, ...],
) -> bool:
    payloads = failed_workspace_state_payloads(source, node)
    if len(payloads) != len(expected_invocations):
        return False
    if not all(
        _failed_provider_workspace_state_is_valid(source, plan, node, payload)
        for payload in payloads
    ):
        return False
    for expected in expected_invocations:
        matches = [
            payload
            for payload in payloads
            if payload_matches_expected_invocation(payload, expected)
        ]
        if len(matches) != 1:
            return False
    return True


def _provider_workspace_state_is_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
) -> bool:
    policy = node.workspace_policy
    if policy is None:
        return False
    if not (
        _workspace_state_header_matches(source, plan, node, payload)
        and _workspace_state_policy_matches(policy.model_dump(mode="json"), payload)
        and _workspace_state_invoker_matches(plan, payload)
        and _workspace_state_git_matches(plan, payload)
        and workspace_invocation_source_matches(source, plan, node, payload)
        and provider_rendered_workspace_files_match(plan, node, payload, source)
    ):
        return False
    workspace = _mapping(payload.get("workspace"))
    if not (
        workspace.get("materialization") == policy.materialization
        and workspace.get("path") is None
        and workspace.get("effective_cwd") is None
    ):
        return False
    if policy.materialization == "snapshot_checkout":
        return workspace.get("writable") is True and _snapshot_result_matches(payload)
    if policy.materialization != "worktree_checkout":
        return False
    if workspace.get("writable") is not True:
        return False
    if workspace.get("lineage_producer") is not True:
        return _disposable_worktree_result_matches(payload)
    return (
        payload.get("role") == "executor"
        and _workspace_result_matches(payload)
        and _workspace_bundle_matches(
            source,
            plan,
            payload,
        )
    )


def _failed_provider_workspace_state_is_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
) -> bool:
    policy = node.workspace_policy
    if policy is None:
        return False
    workspace = _mapping(payload.get("workspace"))
    return (
        _workspace_state_context_matches(source, plan, node, payload, "failed")
        and _workspace_state_policy_matches(policy.model_dump(mode="json"), payload)
        and _failed_workspace_state_invoker_matches(plan, payload)
        and _workspace_state_git_matches(plan, payload)
        and workspace_invocation_source_matches(source, plan, node, payload)
        and provider_rendered_workspace_files_match(plan, node, payload, source)
        and workspace.get("materialization") == policy.materialization
        and workspace.get("path") is None
        and workspace.get("effective_cwd") is None
        and _bool_field_matches(workspace, "writable", policy.writable)
        and _bool_field_matches(
            workspace,
            "lineage_producer",
            policy.lineage_producer,
        )
    )


def _workspace_state_git_matches(
    plan: PreflightExecutionPlan,
    payload: dict[str, object],
) -> bool:
    workspace_source = plan.workspace_source
    if workspace_source is None:
        return False
    git = _mapping(payload.get("git"))
    return (
        git.get("object_format") == workspace_source.object_format
        and git.get("repo_id") == workspace_source.repository_id
        and git.get("run_base_commit") == workspace_source.run_base_commit
        and git.get("source_tree") == workspace_source.source_tree
    )


def _workspace_result_matches(payload: dict[str, object]) -> bool:
    result = _mapping(payload.get("result"))
    return (
        _is_hex_object(result.get("candidate_commit"))
        and _is_hex_object(result.get("result_commit"))
        and _is_hex_object(result.get("candidate_tree"))
        and _is_hex_object(result.get("result_tree"))
        and is_strict_int(result.get("changed_path_count"))
        and result.get("unreachable_provider_objects_scanned") is False
    )


def _snapshot_result_matches(payload: dict[str, object]) -> bool:
    result = _mapping(payload.get("result"))
    changed_path_count = result.get("changed_path_count")
    changed_paths = result.get("changed_paths")
    if not is_strict_int(changed_path_count):
        return False
    snapshot_drift_discarded = changed_path_count > 0
    return (
        result.get("lineage_produced") is False
        and result.get("snapshot_drift_discarded") is snapshot_drift_discarded
        and isinstance(changed_paths, list)
        and all(isinstance(path, str) for path in changed_paths)
        and isinstance(result.get("changed_paths_truncated"), bool)
        and "candidate_commit" not in result
        and "result_commit" not in result
        and "candidate_tree" not in result
        and "result_tree" not in result
        and "bundle" not in payload
    )


def _disposable_worktree_result_matches(payload: dict[str, object]) -> bool:
    result = _mapping(payload.get("result"))
    changed_path_count = result.get("changed_path_count")
    return (
        is_strict_int(changed_path_count)
        and result.get("lineage_produced") is False
        and _is_hex_object(result.get("final_head"))
        and "candidate_commit" not in result
        and "result_commit" not in result
        and "candidate_tree" not in result
        and "result_tree" not in result
        and "bundle" not in payload
    )


def _workspace_bundle_matches(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    payload: dict[str, object],
) -> bool:
    result_ref = _workspace_result_ref(payload)
    result_commit = _workspace_result_commit(payload)
    result_tree = _workspace_result_tree(payload)
    workspace_source = plan.workspace_source
    if (
        result_ref is None
        or result_commit is None
        or result_tree is None
        or workspace_source is None
    ):
        return False
    bundle = _mapping(payload.get("bundle"))
    path = bundle.get("path")
    sha256 = bundle.get("sha256")
    size_bytes = bundle.get("size_bytes")
    if (
        not isinstance(path, str)
        or not isinstance(sha256, str)
        or not is_strict_int(size_bytes)
        or bundle.get("verified") is not True
    ):
        return False
    bundle_path = contained_regular_file(source.run_dir, path)
    if bundle_path is None:
        return False
    try:
        actual_size, actual_sha256 = file_size_and_sha256(bundle_path)
    except OSError:
        return False
    if actual_size != size_bytes:
        return False
    if actual_sha256 != sha256:
        return False
    return workspace_bundle_contains_result_tree(
        workspace_source.git_top_level,
        bundle_path,
        result_ref,
        result_commit,
        result_tree,
        workspace_source.object_format,
    )


def _workspace_result_ref(payload: dict[str, object]) -> str | None:
    refs = _mapping(payload.get("refs"))
    result_ref = refs.get("result")
    if not isinstance(result_ref, str) or not _safe_workspace_result_ref(result_ref):
        return None
    return result_ref


def _workspace_result_commit(payload: dict[str, object]) -> str | None:
    result = _mapping(payload.get("result"))
    result_commit = result.get("result_commit")
    return result_commit if _is_hex_object(result_commit) else None


def _workspace_result_tree(payload: dict[str, object]) -> str | None:
    result = _mapping(payload.get("result"))
    result_tree = result.get("result_tree")
    return result_tree if _is_hex_object(result_tree) else None


def _safe_workspace_result_ref(ref: str) -> bool:
    if (
        not ref.startswith("refs/orchestrator-cli/")
        or not ref.endswith("/result")
        or ref == "@"
        or "@{" in ref
        or ".." in ref
        or "//" in ref
    ):
        return False
    invalid_chars = set(" ~^:?*[\\")
    if any(ord(char) < 32 or ord(char) == 127 or char in invalid_chars for char in ref):
        return False
    parts = ref.split("/")
    return all(
        part
        and part not in {".", ".."}
        and not part.startswith(".")
        and not part.endswith(".")
        and not part.endswith(".lock")
        for part in parts
    )


def _workspace_state_header_matches(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
) -> bool:
    return _workspace_state_context_matches(source, plan, node, payload, "succeeded")


def _workspace_state_context_matches(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
    status: str,
) -> bool:
    return (
        payload.get("version") == SCHEMA_VERSION
        and payload.get("run_id") == source.manifest.run_id
        and payload.get("run_key_name") == source.manifest.run_key_name
        and payload.get("workflow_name") == plan.workflow_name
        and payload.get("workflow_signature") == plan.workflow_signature
        and payload.get("node_id") == node.id
        and payload.get("status") == status
    )


def _workspace_state_policy_matches(
    policy: dict[str, object],
    payload: dict[str, object],
) -> bool:
    return (
        payload.get("workspace_kind") == policy.get("declaration_kind")
        and payload.get("logical_worktree_name") == policy.get("logical_worktree_name")
        and payload.get("clean_start") == policy.get("clean_start")
        and payload.get("worktree_contract") == policy.get("worktree_contract")
    )


def _workspace_state_invoker_matches(
    plan: PreflightExecutionPlan,
    payload: dict[str, object],
) -> bool:
    expected = invoker_workspace_descriptor(plan.runtime_config_snapshot)
    return (
        expected is not None
        and _mapping(payload.get("invoker")) == expected
        and _child_process_environment_matches(expected, payload)
    )


def _failed_workspace_state_invoker_matches(
    plan: PreflightExecutionPlan,
    payload: dict[str, object],
) -> bool:
    expected = invoker_workspace_descriptor(plan.runtime_config_snapshot)
    return (
        expected is not None
        and _mapping(payload.get("invoker")) == expected
        and _failed_child_process_environment_matches(expected, payload)
    )


def _child_process_environment_matches(
    invoker: dict[str, object],
    payload: dict[str, object],
) -> bool:
    if not _controlled_child_environment_required(invoker):
        return True
    child_environment = _mapping(payload.get("child_process_environment"))
    return (
        child_environment.get("required") is True
        and child_environment.get("applied") is True
    )


def _failed_child_process_environment_matches(
    invoker: dict[str, object],
    payload: dict[str, object],
) -> bool:
    if not _controlled_child_environment_required(invoker):
        return True
    child_environment = _mapping(payload.get("child_process_environment"))
    return child_environment.get("required") is True and isinstance(
        child_environment.get("applied"), bool
    )


def _controlled_child_environment_required(invoker: dict[str, object]) -> bool:
    return (
        invoker.get("launch_mode") == "runtime_command_runner"
        and invoker.get("controlled_child_environment") is True
    )
