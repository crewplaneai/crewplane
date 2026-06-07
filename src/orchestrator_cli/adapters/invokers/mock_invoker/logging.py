from __future__ import annotations

import json
from pathlib import Path

from orchestrator_cli.runtime.agent.types import InvocationContext

from .options import MockOptions
from .outputs import OutputResolution


def write_invocation_log(
    options: MockOptions,
    log_file: Path | None,
    output_file: Path,
    context: InvocationContext | None,
    resolution: OutputResolution,
) -> None:
    if log_file is None:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "invoker": "mock",
        "output_mode": options.output_mode,
        "source": resolution.source,
        "fixture_path": (
            str(resolution.fixture_path)
            if resolution.fixture_path is not None
            else None
        ),
        "output_file": str(output_file),
        "node_id": context.node_id if context is not None else None,
        "task_id": context.task_id if context is not None else None,
        "provider": context.provider if context is not None else None,
        "role": context.role if context is not None else None,
        "audit_round_num": context.audit_round_num if context is not None else None,
        "round_num": context.round_num if context is not None else None,
    }
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
