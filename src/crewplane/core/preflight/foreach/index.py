from __future__ import annotations

import posixpath

from crewplane.architecture.contracts import JsonObject, JsonValue
from crewplane.core.workflow.collections import collection_bytes

from ..models import CompiledForeach, PreflightExecutionPlan
from .expansion import generated_id, terminal_node_ids

MAX_INDEX_SIZE = (1 << 64) - 1


def terminal_nodes(template: CompiledForeach) -> list[str]:
    terminals = terminal_node_ids(template)
    return [node_id for node_id in template.execution_order if node_id in terminals]


def projected_index(
    plan: PreflightExecutionPlan, template: CompiledForeach, items: list[JsonObject]
) -> bytes:
    nodes = {node.id: node for node in plan.nodes}
    group = nodes[template.group_id]
    entries: list[JsonObject] = []
    for index, item in enumerate(items):
        results: list[JsonValue] = []
        for local_id in terminal_nodes(template):
            node = nodes[generated_id(template, local_id, index)]
            result: JsonObject = {
                "local_node_id": local_id.removeprefix(f"{template.namespace}."),
                "node_id": node.id,
            }
            for kind, path in (
                ("output", node.artifact_contract.output_path),
                ("findings", node.artifact_contract.findings_path),
            ):
                if path is not None:
                    result[kind] = {
                        "path": posixpath.relpath(
                            path,
                            posixpath.dirname(group.artifact_contract.output_path)
                            or ".",
                        ),
                        "size_bytes": MAX_INDEX_SIZE,
                        "sha256": "0" * 64,
                    }
            results.append(result)
        entries.append({"index": index, "item": item, "results": results})
    try:
        return collection_bytes(
            entries, template.max_items, template.group_id, "projected result index"
        )
    except ValueError as exc:
        raise ValueError(f"{exc}; use smaller items or artifact references") from exc
