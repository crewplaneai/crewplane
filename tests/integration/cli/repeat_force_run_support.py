from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from click.testing import Result
from typer.testing import CliRunner

import crewplane.cli.app as cli
from tests.integration.cli.workflow_runner_support import (
    mock_runner_config,
    run_directories,
)


@dataclass
class RepeatProject:
    root: Path
    config_path: Path
    workflow_path: Path
    config: dict[str, Any]
    workflow: dict[str, Any]
    prompts: dict[str, str]

    def write(self) -> None:
        self.config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        write_markdown_workflow(self.workflow_path, self.workflow, self.prompts)

    def set_count(self, count: int | None) -> None:
        if count is None:
            self.workflow.pop("repeat_force_run_count", None)
        else:
            self.workflow["repeat_force_run_count"] = count
        self.write()

    def run(self, *options: str) -> Result:
        return CliRunner().invoke(
            cli.app,
            [
                "run",
                "--tasks",
                str(self.workflow_path),
                "--config",
                str(self.config_path),
                *options,
            ],
        )

    def manifests(self) -> list[dict[str, Any]]:
        return [
            json.loads((path / "manifests" / "run.json").read_text(encoding="utf-8"))
            for path in run_directories(self.root)
            if (path / "manifests" / "run.json").exists()
        ]


def write_markdown_workflow(
    path: Path, payload: dict[str, Any], prompts: dict[str, str]
) -> None:
    sections = "\n".join(f"## {node}\n{prompt}\n" for node, prompt in prompts.items())
    path.write_text(f"---\n{yaml.safe_dump(payload)}---\n{sections}", encoding="utf-8")


def create_project(
    root: Path, count: int | None = 3, node_count: int = 5
) -> RepeatProject:
    workflow_path = root / ".crewplane" / "workflows" / "repeat.task.md"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    nodes = [
        {
            "id": f"node{index}",
            "mode": "sequential",
            "providers": ["alpha"],
            "needs": [f"node{index - 1}"] if index else [],
        }
        for index in range(node_count)
    ]
    prompts = {
        node["id"]: (
            "Scan the current project. Nothing to clean is a successful result."
            if index == 0
            else f"Read {{{{node{index - 1}.output_path}}}}."
        )
        for index, node in enumerate(nodes)
    }
    project = RepeatProject(
        root,
        root / ".crewplane" / "config.yml",
        workflow_path,
        mock_runner_config().model_dump(mode="json", exclude_none=True),
        {"name": "Repeat", "nodes": nodes},
        prompts,
    )
    project.set_count(count)
    return project


def filesystem_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }
