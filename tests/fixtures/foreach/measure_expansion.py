"""Run once to report the approved 10,000-node expansion memory scenario."""

import json
import sys
import tempfile
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import yaml
from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import PreflightCompileOptions, compile_preflight_preview
from crewplane.core.preflight.foreach.expansion import instantiate_foreach
from crewplane.core.preflight.foreach.index import projected_index
from crewplane.core.preflight.foreach.reconstruction import apply_expansion
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.preflight.source import load_workflow_source_for_preflight


def measure(root: Path) -> None:
    nodes = [{"id": "item", "mode": "input", "source": "{{file:item.json}}"}]
    sections = []
    for index in range(9):
        previous = "item" if index == 0 else f"work{index - 1}"
        nodes.append(
            {
                "id": f"work{index}",
                "mode": "parallel",
                "needs": [previous],
                "providers": ["alpha"],
            }
        )
        sections.append(
            f"## work{index}\nAnalyze {{{{{previous}.output}}}} with {{{{item.output}}}}.\n"
        )
    child = {"name": "Child", "inputs": {"item": "item"}, "nodes": nodes}
    (root / "child.md").write_text(
        "---\n" + yaml.safe_dump(child) + "---\n" + "\n".join(sections),
        encoding="utf-8",
        newline="\n",
    )
    parent = {
        "name": "Memory",
        "nodes": [
            {
                "id": "chunks",
                "mode": "foreach",
                "workflow": "child.md",
                "item_input": "item",
                "max_items": 1000,
                "items": [{"chunk": index} for index in range(1000)],
            }
        ],
    }
    path = root / "parent.md"
    path.write_text(
        "---\n" + yaml.safe_dump(parent) + "---\n## chunks\n",
        encoding="utf-8",
        newline="\n",
    )
    config = Config(
        version="1.0",
        agents={"alpha": AgentConfig(cli_cmd=["mock"])},
        settings=Settings(
            max_concurrent_nodes=8, integrations={"invoker": {"implementation": "mock"}}
        ),
    )
    tracemalloc.start()
    started = perf_counter()
    source = load_workflow_source_for_preflight(path, root)
    snapshot = build_runtime_config_snapshot(config, Console(), no_live=True).snapshot
    preview = compile_preflight_preview(
        source, config, snapshot, PreflightCompileOptions(root, root / ".crewplane")
    )
    if preview.has_errors():
        raise ValueError(preview.diagnostics)
    plan = PreflightExecutionPlan.from_preview(
        preview,
        "measure",
        "measure",
        str(root),
        str(root),
        str(root),
        datetime.now(UTC),
    )
    template = plan.foreach_templates[0]
    assert isinstance(template.items, list)
    batch = instantiate_foreach(plan, template, template.items, {})
    effective = apply_expansion(plan, batch)
    index_bytes = projected_index(effective, template, template.items)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(batch.nodes) == 10_000
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "generated_nodes": len(batch.nodes),
                "items": len(template.items),
                "projected_index_bytes": len(index_bytes),
                "traced_current_bytes": current,
                "traced_peak_bytes": peak,
                "seconds": round(perf_counter() - started, 2),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="foreach-memory-") as directory:
        measure(Path(directory))
