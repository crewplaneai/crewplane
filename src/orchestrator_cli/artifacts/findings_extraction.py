from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from orchestrator_cli.architecture.ports.artifacts import StageTaskSpec

from .failure_artifacts import is_synthetic_invocation_failure

FINDINGS_BLOCK_PATTERN = re.compile(
    r"<!--\s*findings\s*-->\s*(.*?)\s*<!--\s*/findings\s*-->",
    re.DOTALL,
)


class FindingsExtractionError(RuntimeError):
    """Raised when a findings-enabled node does not produce a valid findings block."""


class FindingsSelectionMode(StrEnum):
    DISABLED = "disabled"
    ALL_TASKS = "all_tasks"
    SELECTED_TASKS = "selected_tasks"


@dataclass(frozen=True)
class FindingsSelection:
    mode: FindingsSelectionMode
    selected_task_ids: frozenset[str] = frozenset()

    @classmethod
    def from_stage(
        cls,
        task_specs: tuple[StageTaskSpec, ...],
        findings_enabled: bool,
    ) -> FindingsSelection:
        if not findings_enabled:
            return cls(mode=FindingsSelectionMode.DISABLED)
        if not task_specs:
            return cls(mode=FindingsSelectionMode.ALL_TASKS)
        return cls(
            mode=FindingsSelectionMode.SELECTED_TASKS,
            selected_task_ids=frozenset(
                task.task_id for task in task_specs if task.role == "executor"
            ),
        )

    def should_extract(self, task_id: str, raw_output: str) -> bool:
        if not self.selects_task(task_id):
            return False
        return not is_synthetic_invocation_failure(raw_output)

    def selects_task(self, task_id: str) -> bool:
        if self.mode is FindingsSelectionMode.DISABLED:
            return False
        if self.mode is FindingsSelectionMode.ALL_TASKS:
            return True
        return task_id in self.selected_task_ids


def extract_findings_content(raw_output: str, output_file: Path) -> str:
    matches = FINDINGS_BLOCK_PATTERN.findall(raw_output)
    if len(matches) != 1:
        raise FindingsExtractionError(
            "Expected exactly one findings block in "
            f"'{output_file}'. Use <!-- findings --> ... <!-- /findings -->."
        )
    findings_content = matches[0].strip()
    if findings_content:
        return findings_content
    raise FindingsExtractionError(
        f"Findings block in '{output_file}' must not be empty."
    )


def build_findings_document(findings_sections: list[tuple[str, str]]) -> str:
    if len(findings_sections) == 1:
        return findings_sections[0][1]

    sections: list[str] = []
    for task_id, findings_content in findings_sections:
        sections.append(f"## {task_id}\n\n{findings_content}\n\n---\n\n")
    return "".join(sections)


def build_failure_findings_content(task_id: str) -> str:
    return (
        "- No findings were produced because the executor invocation failed "
        f"for task '{task_id}'. See the full node result for failure details."
    )
