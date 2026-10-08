import hashlib
import json
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from crewplane.adapters.invokers.mock_invoker.invoker import MockAgentInvoker
from crewplane.cli.app import app
from tests.helpers.mock_resume import write_executor_fixture
from tests.helpers.review_checkpoint_cli import write_checkpoint_project


def test_fresh_project_review_artifacts_dedupe_and_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fixtures = tmp_path / "fixtures"
    config, workflow = write_checkpoint_project(tmp_path, fixtures, predecessor=True)
    payload = b"report\r\n\x1a\xff" * 200000
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=30)
    original_invoke = MockAgentInvoker.invoke

    async def create_report(self, *args, **kwargs):
        context = kwargs.get("invocation_context")
        if context is not None and context.node_id == "after":
            (tmp_path / "report.bin").write_bytes(payload)
        await original_invoke(self, *args, **kwargs)

    monkeypatch.setattr(MockAgentInvoker, "invoke", create_report)
    write_executor_fixture(
        fixtures, "after", "## Generated Files\n\n[report](report.bin)\n"
    )
    command = ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    runner = CliRunner()
    before = set(tmp_path.rglob("*"))
    preview = runner.invoke(app, [*command, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert set(tmp_path.rglob("*")) == before
    first = runner.invoke(app, command)
    assert first.exit_code == 0, first.output
    runs = tmp_path / ".crewplane/execution-stages"
    assert len(list(runs.iterdir())) == 1
    stage = next(runs.iterdir())
    manifest = json.loads((stage / "manifests/run.json").read_bytes())
    assert manifest["status"] == "succeeded"
    assert list((stage / "manifests/review-checkpoints").glob("*.json"))
    results = tmp_path / ".crewplane/execution-results" / stage.name
    captured = list(results.rglob("report.bin"))
    assert captured and all(path.read_bytes() == payload for path in captured)
    for node_file in (stage / "manifests/nodes").glob("*.json"):
        node = json.loads(node_file.read_bytes())
        for artifact in node.get("artifacts", []):
            data = (results / artifact["relative_path"]).read_bytes()
            assert len(data) == artifact["size_bytes"]
            assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
    assert list(stage.rglob("summary.md"))
    second = runner.invoke(app, command)
    assert second.exit_code == 0, second.output
    assert "Identical context detected" in second.output
    assert len(list(runs.iterdir())) == 1
    forced = runner.invoke(app, [*command, "--force"])
    assert forced.exit_code == 0, forced.output
    assert len(list(runs.iterdir())) == 2
    frontmatter, sections = workflow.read_text().split("---", 2)[1:]
    specification = yaml.safe_load(frontmatter)
    specification["repeat_force_run_count"] = 2
    workflow.write_text(
        "---\n" + yaml.safe_dump(specification) + "---" + sections,
        encoding="utf-8",
        newline="\n",
    )
    repeated = runner.invoke(app, command)
    assert repeated.exit_code == 0, repeated.output
    assert len(list(runs.iterdir())) == 4


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_windows_incomplete_attempts_start_fresh(tmp_path, monkeypatch, status):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("crewplane.core.platform.platform.system", lambda: "Windows")
    config, workflow = write_checkpoint_project(tmp_path, tmp_path / "fixtures")
    command = ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    runner = CliRunner()
    first = runner.invoke(app, command)
    assert first.exit_code == 0, first.output
    stages = tmp_path / ".crewplane/execution-stages"
    prior = next(stages.iterdir())
    manifest_path = prior / "manifests/run.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["status"] = status
    manifest["failure_message" if status == "failed" else "cancel_reason"] = (
        "interrupted attempt"
    )
    manifest_path.write_text(json.dumps(manifest))
    second = runner.invoke(app, command)
    assert second.exit_code == 0, second.output
    current = next(path for path in stages.iterdir() if path != prior)
    for node_path in (current / "manifests/nodes").glob("*.json"):
        assert json.loads(node_path.read_bytes()).get("resume_origin") is None
    assert "Resuming" not in second.output
