from __future__ import annotations

import sys
from pathlib import Path

import yaml

from crewplane.version import SCHEMA_VERSION


def write_mock_config(path: Path, fixture_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": SCHEMA_VERSION,
                "agents": {
                    "alpha": {
                        "cli_cmd": [sys.executable],
                        "default_model": "model-a",
                    }
                },
                "settings": {
                    "integrations": {
                        "invoker": {
                            "implementation": "mock",
                            "options": {
                                "observation_delay_seconds": 0,
                                "output_mode": "file",
                                "output_dir": str(fixture_dir),
                                "strict_file_mode": True,
                            },
                        },
                        "ui": {"implementation": "none", "options": {}},
                        "artifacts": {
                            "implementation": "filesystem",
                            "options": {
                                "log_cli_output": True,
                            },
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def write_executor_fixture(fixture_dir: Path, node_id: str, content: str) -> None:
    fixture_path = fixture_dir / node_id / "alpha_executor_0_round1.md"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(content, encoding="utf-8")


def write_review_loop_fixtures(fixture_dir: Path) -> None:
    review_dir = fixture_dir / "review.iterate" / "review-audit-round-1"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "alpha_executor_0_round1.md").write_text(
        "Reviewed implementation candidate.\n",
        encoding="utf-8",
    )
    (review_dir / "reviewer-round-1.md").write_text(
        "\n".join(
            [
                "Review accepted.",
                "",
                "## Major Issues",
                "None",
                "",
                "## Minor Issues",
                "None",
                "",
                "## Nitpicks",
                "None",
                "",
                "---",
                "VERDICT: NO_FINDINGS",
                "",
            ]
        ),
        encoding="utf-8",
    )
