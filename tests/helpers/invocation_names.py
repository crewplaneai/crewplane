from pathlib import Path

from crewplane.architecture.contracts.artifacts import (
    MAX_ARTIFACT_PATH_COMPONENT_CHARS,
    build_task_round_filename,
    candidate_identity_path,
    review_metadata_path,
)


def boundary_alias(mode: str, depth: int, role: str, index: int = 0) -> str:
    round_num = depth + 1 if mode == "review" else depth
    task = Path(build_task_round_filename(f"a_{role}_{index}", round_num))
    name = (
        candidate_identity_path(task).name
        if mode == "review" and role == "executor"
        else review_metadata_path(task).name
        if mode == "review"
        else task.name
    )
    return "a" * (1 + MAX_ARTIFACT_PATH_COMPONENT_CHARS - len(name))
