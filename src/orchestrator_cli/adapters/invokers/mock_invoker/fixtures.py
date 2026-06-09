from __future__ import annotations

from pathlib import Path

from orchestrator_cli.architecture.contracts import InvocationContext


def fixture_candidates(
    output_dir: Path, context: InvocationContext | None
) -> tuple[Path, ...]:
    """Return candidate fixture paths from most specific to least specific."""

    candidates: list[Path] = []
    if context is not None:
        node_dir = output_dir / context.node_id
        role_name = context.role.strip().lower()
        grouped_node_dir = (
            node_dir / f"review-audit-round-{context.audit_round_num}"
            if context.audit_round_num is not None
            else None
        )
        search_dirs = [
            candidate
            for candidate in (grouped_node_dir, node_dir)
            if candidate is not None
        ]
        for search_dir in search_dirs:
            if context.round_num is not None:
                candidates.append(
                    search_dir / f"{context.task_id}_round{context.round_num}.md"
                )
                candidates.append(
                    search_dir / f"{role_name}-round-{context.round_num}.md"
                )
            candidates.append(search_dir / f"{context.task_id}.md")
            candidates.append(search_dir / f"{role_name}.md")
            if search_dir == grouped_node_dir:
                candidates.append(search_dir / f"default-{role_name}.md")
        candidates.append(output_dir / f"{context.node_id}.md")
        candidates.append(output_dir / f"default-{role_name}.md")
    candidates.append(output_dir / "default.md")
    return tuple(candidates)
