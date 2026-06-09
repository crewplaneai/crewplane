from __future__ import annotations

from rich.console import Console

from orchestrator_cli.core.preflight import (
    PreflightCompilationPreview,
    PreflightExecutionNode,
    ProviderRecord,
)

__all__ = [
    "print_dry_run_plan",
    "preview_topological_waves",
]


def print_dry_run_plan(preview: PreflightCompilationPreview, console: Console) -> None:
    console.print("[yellow]Dry run mode[/] — showing DAG execution plan:")
    waves = preview_topological_waves(preview)
    node_index = {node.id: node for node in preview.nodes}

    for wave_number, wave in enumerate(waves, start=1):
        console.rule(f"Wave {wave_number}")
        for node_id in wave:
            node = node_index[node_id]
            needs = ", ".join(node.dependencies) if node.dependencies else "(root)"
            console.print(f"  Node: {node.id} ({node.mode})")
            console.print(f"    needs: {needs}")
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


def _input_source_label(
    preview: PreflightCompilationPreview,
    node: PreflightExecutionNode,
) -> str:
    for token in preview.token_catalog:
        if token.node_id == node.id and token.token_kind == "file":
            return token.raw_token
    return node.input_content_ref or "(unresolved)"
