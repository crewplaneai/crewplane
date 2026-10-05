from pathlib import Path

import yaml

from crewplane.version import SCHEMA_VERSION
from tests.helpers.mock_resume import (
    write_executor_fixture,
    write_mock_config,
    write_review_loop_fixtures,
)


def write_checkpoint_project(
    root: Path, fixtures: Path, predecessor: bool = False
) -> tuple[Path, Path]:
    config = root / ".crewplane/config.yml"
    workflow = root / ".crewplane/workflows/checkpoint.task.md"
    write_mock_config(config, fixtures)
    workflow.parent.mkdir(parents=True, exist_ok=True)
    nodes = []
    if predecessor:
        (root / "requirements.md").write_text("Requirements")
        nodes.append(
            {
                "id": "requirements",
                "mode": "input",
                "source": "{{file:requirements.md}}",
            }
        )
    nodes.extend(
        [
            {
                "id": "review.iterate",
                "mode": "sequential",
                "depth": 2,
                "audit_rounds": 3,
                "needs": ["requirements"] if predecessor else [],
                "providers": [
                    {"provider": "alpha", "role": role}
                    for role in ("executor", "reviewer")
                ],
            },
            {
                "id": "after",
                "mode": "sequential",
                "needs": ["review.iterate"],
                "providers": ["alpha"],
            },
        ]
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "name": "Checkpoint Resume",
        "nodes": nodes,
    }
    workflow.write_text(
        "---\n"
        + yaml.safe_dump(payload, sort_keys=False)
        + "---\n\n## review.iterate\n\nImplement and review.\n\n## after\n\nUse {{review.iterate.output}}.\n"
    )
    write_review_loop_fixtures(fixtures)
    write_executor_fixture(fixtures, "after", "Consumed selected candidate.\n")
    return config, workflow
