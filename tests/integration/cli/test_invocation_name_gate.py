import json

import pytest
import yaml
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.version import SCHEMA_VERSION
from tests.helpers.invocation_names import boundary_alias
from tests.integration.architecture.preflight_plan.helpers import mock_config


@pytest.mark.parametrize(
    ("mode", "role"),
    [
        ("parallel", "executor"),
        ("sequential", "executor"),
        ("review", "executor"),
        ("review", "reviewer"),
    ],
)
@pytest.mark.parametrize("oversize", [False, True])
def test_cli_generated_names_are_checked_before_dispatch_and_publish_at_boundary(
    tmp_path, monkeypatch, mode, role, oversize
):
    monkeypatch.chdir(tmp_path)
    alias = boundary_alias(mode, 1, role) + ("a" if oversize else "")
    config = mock_config()
    config.agents[alias] = config.agents["mock"].model_copy()
    state = tmp_path / ".crewplane"
    state.mkdir()
    (state / "config.yml").write_text(
        yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8"
    )
    providers = [{"provider": alias, "role": role}]
    if mode == "review":
        providers.append(
            {
                "provider": "mock",
                "role": "reviewer" if role == "executor" else "executor",
            }
        )
        if role == "reviewer":
            providers.reverse()
    frontmatter = {
        "schema_version": SCHEMA_VERSION,
        "name": "Name gate",
        "nodes": [
            {
                "id": "build",
                "mode": "parallel" if mode == "parallel" else "sequential",
                "providers": providers,
            }
        ],
    }
    workflow = tmp_path / "workflow.task.md"
    workflow.write_text(
        "---\n" + yaml.safe_dump(frontmatter) + "---\n## build\nProduce output.\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    for args in (
        ["validate", str(workflow)],
        ["run", "--tasks", str(workflow), "--dry-run"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == (1 if oversize else 0), result.output
        assert not (state / "execution-stages").exists()
    result = runner.invoke(app, ["run", "--tasks", str(workflow), "--no-live"])
    assert result.exit_code == (1 if oversize else 0), result.output
    (run,) = (state / "execution-stages").iterdir()
    if oversize:
        assert "180-character budget" in result.output
        assert not (run / "build").exists()
        assert not (run / "manifests/run.json").exists()
    else:
        task = f"{alias}_{role}_0_round1.md"
        assert (run / "build" / task).is_file()
        if mode == "review":
            suffix = ".candidate.json" if role == "executor" else ".review.json"
            assert (run / "build" / task).with_suffix(suffix).is_file()
            assert list((run / "build").glob("*.review.json"))
        manifest = json.loads((run / "manifests/run.json").read_bytes())
        assert manifest["status"] == "succeeded"
        assert (
            "Produce output."
            in (state / "execution-results" / run.name / "build-result.md").read_text()
        )
        events = [
            json.loads(line)
            for line in (run / "logs/events.ndjson").read_text().splitlines()
        ]
        assert any(
            event.get("provider") == alias
            and event.get("task_id") == f"{alias}_{role}_0"
            for event in events
        )
