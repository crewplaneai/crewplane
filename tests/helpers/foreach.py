from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

from crewplane.artifacts import OutputManager
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import PreflightCompileOptions, compile_preflight_preview
from crewplane.core.preflight.models import (
    PreflightCompilationPreview,
    PreflightExecutionPlan,
)
from crewplane.core.preflight.source import load_workflow_source_for_preflight


def copy_foreach_fixture(root: Path) -> Path:
    source = Path(__file__).parents[1] / "fixtures" / "foreach"
    root.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.md"):
        shutil.copyfile(path, root / path.name)
    (root / "request.txt").write_bytes(b"Review both chunks.\n")
    return root / "parent.md"


def workflow_document(path: Path) -> tuple[dict[str, Any], str]:
    _, frontmatter, body = path.read_text(encoding="utf-8").split("---", 2)
    return yaml.safe_load(frontmatter), body


def write_workflow_document(path: Path, payload: dict[str, Any], body: str) -> None:
    path.write_text(
        "---\n" + yaml.safe_dump(payload, sort_keys=False) + "---\n" + body,
        encoding="utf-8",
        newline="\n",
    )


def foreach_config(**settings: object) -> Config:
    return Config(
        version="1.0",
        agents={"alpha": AgentConfig(cli_cmd=["mock"])},
        settings=Settings.model_validate(
            {
                "max_concurrent_nodes": 1,
                "integrations": {"invoker": {"implementation": "mock"}},
                **settings,
            }
        ),
    )


def compile_foreach_file(
    path: Path, config: Config | None = None
) -> PreflightCompilationPreview:
    config = config or foreach_config()
    source = load_workflow_source_for_preflight(path, path.parent)
    snapshot = build_runtime_config_snapshot(config, Console(), no_live=True).snapshot
    return compile_preflight_preview(
        source,
        config,
        snapshot,
        PreflightCompileOptions(path.parent, path.parent / ".crewplane"),
    )


def freeze_foreach(
    preview: PreflightCompilationPreview, output: OutputManager
) -> PreflightExecutionPlan:
    assert not preview.has_errors(), preview.diagnostics
    plan = PreflightExecutionPlan.from_preview(
        preview,
        output.run_id,
        output.run_key_name,
        str(output.base_dir),
        str(output.stages_dir),
        str(output.stages_dir / "manifests"),
        datetime(2026, 10, 28, tzinfo=UTC),
    )
    for ref, data in preview.static_file_payloads.items():
        output.write_preflight_static_file(ref, data)
    output.write_preflight_json("execution-plan.json", plan)
    return plan


def compile_literal_foreach(
    root: Path,
    items: str = "[{x: 1}, {x: 1}]",
    max_items: int | None = None,
    **settings: object,
):
    (root / "child.md").write_text(
        """---
name: Child
inputs: {item: item}
nodes:
  - {id: item, mode: input, source: "{{file:missing.json}}"}
  - {id: analyze, mode: parallel, needs: [item], providers: [alpha]}
  - {id: independent, mode: parallel, providers: [alpha]}
---
## analyze
Read {{item.output}}.
## independent
Independent root.
""",
        encoding="utf-8",
        newline="\n",
    )
    parent = root / "parent.md"
    parent.write_text(
        """---
name: Parent
nodes:
  - id: chunks
    mode: foreach
    workflow: child.md
    item_input: item
    items: """
        + items
        + (f"\n    max_items: {max_items}" if max_items is not None else "")
        + """
---
## chunks
<!-- comment -->
""",
        encoding="utf-8",
        newline="\n",
    )
    return compile_foreach_file(parent, foreach_config(**settings))
