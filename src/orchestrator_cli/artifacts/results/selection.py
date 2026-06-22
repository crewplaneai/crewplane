from __future__ import annotations

from pathlib import Path

from orchestrator_cli.architecture.ports.artifacts import StageTaskSpec


def latest_round_files(stage_dir: Path) -> dict[str, Path]:
    latest_by_task: dict[str, tuple[int, int, Path]] = {}
    for audit_round_num, md_file in candidate_markdown_files(stage_dir):
        task_id, round_num = parse_task_round(md_file.stem)
        previous = latest_by_task.get(task_id)
        if previous is not None and (audit_round_num, round_num) <= (
            previous[0],
            previous[1],
        ):
            continue
        latest_by_task[task_id] = (audit_round_num, round_num, md_file)
    return {task_id: file_path for task_id, (_, _, file_path) in latest_by_task.items()}


def candidate_markdown_files(stage_dir: Path) -> list[tuple[int, Path]]:
    audit_dirs = sorted(
        (
            candidate
            for candidate in stage_dir.glob("review-audit-round-*")
            if candidate.is_dir()
        ),
        key=lambda candidate: parse_audit_round(candidate.name),
    )
    if not audit_dirs:
        return [(0, md_file) for md_file in stage_dir.glob("*.md")]

    candidates: list[tuple[int, Path]] = []
    for audit_dir in audit_dirs:
        audit_round_num = parse_audit_round(audit_dir.name)
        candidates.extend(
            (audit_round_num, md_file) for md_file in audit_dir.glob("*.md")
        )
    return candidates


def ordered_task_ids(
    selected_files: dict[str, Path],
    task_specs: tuple[StageTaskSpec, ...],
) -> list[str]:
    if not task_specs:
        return sorted(selected_files)

    ordered_task_ids: list[str] = []
    seen: set[str] = set()
    for task_spec in task_specs:
        if task_spec.task_id not in selected_files or task_spec.task_id in seen:
            continue
        ordered_task_ids.append(task_spec.task_id)
        seen.add(task_spec.task_id)

    for task_id in sorted(selected_files):
        if task_id in seen:
            continue
        ordered_task_ids.append(task_id)
    return ordered_task_ids


def is_raw_input_stage(selected_files: dict[str, Path]) -> bool:
    return set(selected_files) == {"input"}


def parse_task_round(stem: str) -> tuple[str, int]:
    if "_round" not in stem:
        return stem, 0
    try:
        task_id, round_str = stem.rsplit("_round", 1)
        return task_id, int(round_str)
    except ValueError:
        return stem, 0


def parse_audit_round(dir_name: str) -> int:
    prefix = "review-audit-round-"
    if not dir_name.startswith(prefix):
        return 0
    try:
        return int(dir_name[len(prefix) :])
    except ValueError:
        return 0
