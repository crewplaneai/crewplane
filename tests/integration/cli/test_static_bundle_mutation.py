import json
import sys
from pathlib import Path

import yaml
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.version import SCHEMA_VERSION
from tests.helpers.platforms import requires_resume_support


@requires_resume_support
def test_provider_mutation_fails_before_downstream_dispatch_and_clean_run_retries(
    tmp_path, monkeypatch
):
    script = (
        Path(__file__).resolve().parents[2]
        / "fixtures/process/static_bundle_mutator.py"
    )
    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".crewplane"
    state.mkdir()
    config = {
        "version": SCHEMA_VERSION,
        "agents": {"alpha": {"cli_cmd": [sys.executable, str(script)]}},
        "settings": {"integrations": {"ui": {"implementation": "none"}}},
    }
    (state / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (tmp_path / "source.txt").write_bytes(b"Frozen input.\r\n")
    workflow = tmp_path / "workflow.task.md"
    workflow.write_text(
        f'''---
schema_version: "{SCHEMA_VERSION}"
name: Bundle integrity
nodes:
  - id: mutate
    mode: sequential
    providers: [alpha]
  - id: consume
    mode: sequential
    needs: [mutate]
    providers: [alpha]
  - id: after
    mode: sequential
    needs: [consume]
    providers: [alpha]
---
## mutate
Mutate bundle.
## consume
{{{{file:source.txt}}}}
## after
After consumption.
''',
        encoding="utf-8",
        newline="\n",
    )
    args = ["run", "--tasks", str(workflow), "--no-live"]
    runner = CliRunner()
    failed = runner.invoke(app, args)
    assert failed.exit_code == 1, failed.output
    (first,) = (state / "execution-stages").iterdir()
    assert (tmp_path / "invocations.txt").read_text().splitlines() == ["Mutate bundle."]
    assert not (first / "consume/alpha_executor_0_round1.md").exists()
    assert not (state / "execution-results" / first.name / "consume-result.md").exists()
    assert json.loads((first / "manifests/run.json").read_bytes())["status"] == "failed"
    (tmp_path / "clean-run").touch()
    clean = runner.invoke(app, args)
    assert clean.exit_code == 0, clean.output
    (second,) = [
        path for path in (state / "execution-stages").iterdir() if path != first
    ]
    manifest = json.loads((second / "manifests/run.json").read_bytes())
    assert manifest["status"] == "succeeded"
    assert manifest["resumed_nodes"] == ["mutate"]
    assert (tmp_path / "invocations.txt").read_text().splitlines() == [
        "Mutate bundle.",
        "Frozen input.",
        "After consumption.",
    ]
    assert (second / "consume/alpha_executor_0_round1.md").exists()
    assert not list((second / "consume").glob("*checkpoint*"))
