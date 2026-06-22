from __future__ import annotations

from rich.console import Console

from orchestrator_cli.core.preflight import (
    PreflightCompilationPreview,
    PreflightExecutionNode,
    ProviderRecord,
)
from orchestrator_cli.core.preflight.workspace_observability import (
    workspace_observability_descriptor,
)

__all__ = [
    "print_dry_run_plan",
    "preview_topological_waves",
]


def print_dry_run_plan(preview: PreflightCompilationPreview, console: Console) -> None:
    console.print("[yellow]Dry run mode[/] — showing DAG execution plan:")
    _print_workspace_summary(preview, console)
    waves = preview_topological_waves(preview)
    node_index = {node.id: node for node in preview.nodes}

    for wave_number, wave in enumerate(waves, start=1):
        console.rule(f"Wave {wave_number}")
        for node_id in wave:
            node = node_index[node_id]
            needs = ", ".join(node.dependencies) if node.dependencies else "(root)"
            console.print(f"  Node: {node.id} ({node.mode})")
            console.print(f"    needs: {needs}")
            _print_node_workspace_summary(node, console)
            if node.mode == "input":
                console.print(f"      - source: {_input_source_label(preview, node)}")
                continue
            for provider in node.provider_records:
                console.print(_format_provider_dry_run_line(provider), markup=False)


def preview_topological_waves(
    preview: PreflightCompilationPreview,
) -> list[list[str]]:
    node_order = {
        node_id: index for index, node_id in enumerate(preview.execution_order)
    }
    remaining = {node.id: set(node.dependencies) for node in preview.nodes}
    waves: list[list[str]] = []
    while remaining:
        ready = sorted(
            (
                node_id
                for node_id, dependencies in remaining.items()
                if not dependencies
            ),
            key=lambda node_id: node_order.get(node_id, len(node_order)),
        )
        if not ready:
            raise ValueError("Compiled preview dependency graph contains a cycle.")
        waves.append(ready)
        for node_id in ready:
            del remaining[node_id]
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return waves


def _format_provider_dry_run_line(provider: ProviderRecord) -> str:
    model = provider.model or "provider default"
    role_suffix = f" [{provider.role}]" if provider.role else ""
    return f"      - {provider.provider}{role_suffix} ({model})"


def _print_workspace_summary(
    preview: PreflightCompilationPreview,
    console: Console,
) -> None:
    descriptor = workspace_observability_descriptor(preview)
    if descriptor is None:
        return
    source = descriptor.get("source")
    rendered = descriptor.get("rendered_files")
    invoker = descriptor.get("invoker")
    cleanup = descriptor.get("cleanup")
    console.print("Workspace: enabled", markup=False)
    console.print(
        f"  contract: {_workspace_contract_label(descriptor)}",
        markup=False,
    )
    if isinstance(source, dict):
        console.print(
            "  source: "
            f"commit={source.get('run_base_commit')} "
            f"tree={source.get('source_tree')} "
            f"object_format={source.get('object_format')} "
            f"clean_start={source.get('clean_start')}",
            markup=False,
        )
    if isinstance(invoker, dict):
        console.print(
            "  invoker: "
            f"{invoker.get('implementation')} "
            f"launch={invoker.get('launch_mode')} "
            f"controlled_env={invoker.get('controlled_child_environment')}",
            markup=False,
        )
    if isinstance(rendered, dict):
        console.print(
            "  rendered workspace files: "
            f"{rendered.get('locator_count')} "
            f"(project_initial={rendered.get('project_initial')}, "
            f"runtime_dynamic={rendered.get('runtime_dynamic')})",
            markup=False,
        )
    if isinstance(cleanup, dict):
        console.print(
            "  cleanup: "
            f"cleanup_on_success={cleanup.get('cleanup_on_success')} "
            f"cache_root_configured={cleanup.get('cache_root_configured')}",
            markup=False,
        )


def _workspace_contract_label(descriptor: dict[str, object]) -> object:
    contract = descriptor.get("worktree_contract")
    if isinstance(contract, dict):
        return contract.get("mode")
    return contract


def _print_node_workspace_summary(
    node: PreflightExecutionNode,
    console: Console,
) -> None:
    policy = node.workspace_policy
    if policy is None or not policy.enabled:
        return
    console.print(
        "    workspace: "
        f"{policy.declaration_kind} "
        f"name={policy.logical_worktree_name} "
        f"source={policy.source_kind}"
        f"{':' + policy.source_node_id if policy.source_node_id else ''} "
        f"clean_start={policy.clean_start} "
        f"materialization={policy.materialization} "
        f"result={_node_workspace_result(node)}",
        markup=False,
    )
    if policy.setup is not None:
        console.print(
            "    setup: "
            f"profile={policy.setup.profile_name} "
            f"commands={len(policy.setup.commands)}",
            markup=False,
        )
    if policy.branch_export.create_branch:
        console.print(
            "    branch export: "
            f"name={policy.branch_export.branch_name or '(generated)'}",
            markup=False,
        )


def _node_workspace_result(node: PreflightExecutionNode) -> str:
    if node.mode == "input":
        return "static_file"
    policy = node.workspace_policy
    if policy is not None and policy.lineage_producer:
        return "deterministic_commit_tree"
    if policy is not None and policy.declaration_kind == "snapshot":
        return "discarded_snapshot_drift"
    return "discarded_drift_summary"


def _input_source_label(
    preview: PreflightCompilationPreview,
    node: PreflightExecutionNode,
) -> str:
    for token in preview.token_catalog:
        if token.node_id == node.id and token.token_kind == "file":
            return token.raw_token
    return node.input_content_ref or "(unresolved)"
