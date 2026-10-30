import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from typer.testing import CliRunner

import crewplane.cli.app as cli
from tests.helpers.foreach import (
    compile_literal_foreach,
    copy_foreach_fixture,
    workflow_document,
    write_workflow_document,
)
from tests.helpers.mock_resume import write_executor_fixture, write_mock_config
from tests.helpers.platforms import requires_resume_support


def records(root: Path) -> list[tuple[Path, dict[str, object]]]:
    return [
        (path.parent.parent, json.loads(path.read_bytes()))
        for path in sorted(
            (root / ".crewplane/execution-stages").glob("*/manifests/run.json")
        )
    ]


@requires_resume_support
@pytest.mark.parametrize("shift_child_lines", [False, True])
@pytest.mark.parametrize("literal", [False, True])
def test_mock_foreach_validate_dry_run_fail_resume_skip_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shift_child_lines: bool,
    literal: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    clock = Mock()
    clock.now.side_effect = [
        datetime(2026, 10, 28, 12) + timedelta(seconds=i) for i in range(10)
    ]
    monkeypatch.setattr("crewplane.artifacts.directory_manager.datetime", clock)
    workflow = copy_foreach_fixture(tmp_path)
    if literal:
        payload, body = workflow_document(workflow)
        payload["nodes"][2]["items"] = [{"chunk": "a"}, {"chunk": "b"}]
        del payload["nodes"][2]["max_items"]
        write_workflow_document(workflow, payload, body)
    fixtures = tmp_path / "outputs"
    config = tmp_path / ".crewplane/config.yml"
    write_mock_config(config, fixtures)
    payload = yaml.safe_load(config.read_bytes())
    payload["settings"]["max_concurrent_nodes"] = 1
    config.write_text(yaml.safe_dump(payload), encoding="utf-8", newline="\n")
    write_executor_fixture(
        fixtures,
        "planner",
        '<!-- findings -->\n[{"chunk":"a"},{"chunk":"b"}]\n<!-- /findings -->',
    )
    write_executor_fixture(
        fixtures, "chunks.i0.part.analyze", "<!-- findings -->A<!-- /findings -->"
    )
    write_executor_fixture(fixtures, "summarize", "Summary")
    runner = CliRunner()
    arguments = ["--tasks", str(workflow), "--config", str(config)]
    valid = runner.invoke(cli.app, ["validate", str(workflow), "--config", str(config)])
    assert valid.exit_code == 0, valid.output
    assert ("items=2" if literal else "0..4") in valid.output
    assert "budget=10000" in valid.output
    dry = runner.invoke(cli.app, ["run", *arguments, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert f"generated-node bound={4 if literal else 8}" in dry.output
    assert not (tmp_path / ".crewplane/execution-stages").exists()
    failed = runner.invoke(cli.app, ["run", *arguments, "--no-live"])
    assert failed.exit_code == 1, (failed.output, failed.exception)
    (first,) = records(tmp_path)
    assert first[1]["status"] == "failed"
    base = (first[0] / "preflight/execution-plan.json").read_bytes()
    assert [node["id"] for node in json.loads(base)["nodes"]] == [
        "request",
        "planner",
        "chunks",
        "summarize",
    ]
    write_executor_fixture(
        fixtures, "chunks.i1.part.analyze", "<!-- findings -->B<!-- /findings -->"
    )
    if shift_child_lines:
        child = tmp_path / "analyze.md"
        payload, body = workflow_document(child)
        write_workflow_document(child, payload, "\n\n" + body)
    resumed = runner.invoke(cli.app, ["run", *arguments, "--no-live"])
    assert resumed.exit_code == 0, (resumed.output, resumed.exception)
    first, second = records(tmp_path)
    assert second[1]["status"] == "succeeded"
    assert second[1]["workflow_signature"] == first[1]["workflow_signature"]
    restored = json.loads((second[0] / "preflight/execution-plan.json").read_bytes())
    assert restored["render_plans"] == json.loads(base)["render_plans"]
    producer = next(
        render for render in restored["render_plans"] if render["node_id"] == "planner"
    )
    for stream in producer["streams"]:
        text = "".join(fragment.get("text") or "" for fragment in stream["fragments"])
        assert ("at most 4 items." in text) == (not literal)
    assert set(second[1]["resumed_nodes"]) == {
        "request",
        "planner",
        "chunks.i0.item",
        "chunks.i0.part.analyze",
        "chunks.i1.item",
    }
    assert not (
        second[0] / "chunks.i0.part.analyze/alpha_executor_0_round1.md"
    ).exists()
    assert (second[0] / "chunks.i1.part.analyze/alpha_executor_0_round1.md").exists()
    assert (first[0] / "preflight/execution-plan.json").read_bytes() == base
    index = json.loads(
        (
            tmp_path
            / ".crewplane/execution-results"
            / second[0].name
            / "chunks-result.md"
        ).read_bytes()
    )
    assert [entry["index"] for entry in index] == [0, 1]
    assert [entry["item"] for entry in index] == [{"chunk": "a"}, {"chunk": "b"}]
    if shift_child_lines:
        payload, body = workflow_document(child)
        write_workflow_document(child, payload, "\n\n" + body)
    skipped = runner.invoke(cli.app, ["run", *arguments, "--no-live"])
    assert skipped.exit_code == 0, skipped.output
    assert len(records(tmp_path)) == 2
    forced = runner.invoke(cli.app, ["run", *arguments, "--no-live", "--force"])
    assert forced.exit_code == 0, (forced.output, forced.exception)
    third = records(tmp_path)[-1]
    assert third[1]["resumed_nodes"] == []
    assert third[1]["workflow_signature"] == second[1]["workflow_signature"]
    for node_id in ("planner", "chunks.i0.part.analyze", "chunks.i1.part.analyze"):
        assert (third[0] / node_id / "alpha_executor_0_round1.md").exists()
    assert (
        json.loads(
            (third[0] / "preflight/expansions/chunks/expansion.json").read_bytes()
        )["state"]["digest"]
        == json.loads(
            (first[0] / "preflight/expansions/chunks/expansion.json").read_bytes()
        )["state"]["digest"]
    )


def test_mock_empty_literal_validate_dry_run_skip_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    compile_literal_foreach(tmp_path, "[]")
    workflow = tmp_path / "parent.md"
    config = tmp_path / ".crewplane/config.yml"
    write_mock_config(config, tmp_path / "outputs")
    payload = yaml.safe_load(config.read_bytes())
    payload["settings"]["max_concurrent_nodes"] = 1
    config.write_text(yaml.safe_dump(payload), encoding="utf-8", newline="\n")
    runner = CliRunner()
    arguments = ["--tasks", str(workflow), "--config", str(config), "--no-live"]
    valid = runner.invoke(cli.app, ["validate", str(workflow), "--config", str(config)])
    assert valid.exit_code == 0, valid.output
    assert "items=0" in valid.output
    dry = runner.invoke(cli.app, ["run", *arguments, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "generated-node bound=0" in dry.output
    assert not (tmp_path / ".crewplane/execution-stages").exists()
    for suffix in ([], [], ["--force"]):
        result = runner.invoke(cli.app, ["run", *arguments, *suffix])
        assert result.exit_code == 0, result.output
        if not suffix:
            assert len(records(tmp_path)) == 1
    assert len(records(tmp_path)) == 2
    for run_dir, manifest in records(tmp_path):
        assert manifest["status"] == "succeeded"
        result = tmp_path / ".crewplane/execution-results" / run_dir.name
        assert (result / "chunks-result.md").read_bytes() == b"[]"
        assert (
            run_dir / "preflight/expansions/chunks/items.json"
        ).read_bytes() == b"[]"
        expansion = json.loads(
            (run_dir / "preflight/expansions/chunks/expansion.json").read_bytes()
        )
        assert expansion["nodes"] == []
        assert expansion["state"]["items"] == []
        assert not list(run_dir.glob("chunks.i*"))
