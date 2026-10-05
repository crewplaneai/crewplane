from typing import Any

import yaml

from tests.unit.packaging.release_surfaces_support import read_text


def load_workflow(filename: str) -> dict[str, Any]:
    return yaml.load(
        read_text(".github", "workflows", filename), Loader=yaml.BaseLoader
    )


def workflow_step_run(job: dict[str, Any], step_id: str) -> str:
    steps = {step.get("id"): step for step in job["steps"] if isinstance(step, dict)}
    return str(steps[step_id].get("run", ""))
