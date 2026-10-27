import io
import json
from pathlib import Path

import pytest
import typer

import crewplane.cli.app as cli
from crewplane.artifacts.naming import node_state_relative_path
from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.artifacts.run_history import find_same_context_runs
from crewplane.core.execution_state import RunManifest
from crewplane.core.preflight import PreflightExecutionPlan
from tests.helpers.import_needs import write_import_needs_workflow
from tests.helpers.mock_resume import write_executor_fixture, write_mock_config
from tests.helpers.platforms import requires_resume_support
from tests.integration.cli.cli_workflow_helpers import ConsoleFactory

_PROVIDER_IDS = ("plan", "implement", "doc.review", "doc.audit", "doc.summarize")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    workflow = write_import_needs_workflow(tmp_path)
    config = tmp_path / ".crewplane" / "config.yml"
    fixtures = tmp_path / "fixtures"
    write_mock_config(config, fixtures)
    for node_id in _PROVIDER_IDS:
        write_executor_fixture(fixtures, node_id, f"{node_id} result\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "Console", ConsoleFactory(file=io.StringIO(), width=120))
    return workflow, config


def _run(project: tuple[Path, Path], force: bool = False) -> None:
    workflow, config = project
    cli.run(tasks_file=workflow, config_file=config, force=force, no_live=True)


def _runs(root: Path) -> list[Path]:
    return sorted((root / ".crewplane" / "execution-stages").iterdir())


def _events(run: Path) -> list[tuple[str, str | None]]:
    records = [
        json.loads(line)
        for line in (run / "logs" / "events.ndjson").read_text("utf-8").splitlines()
    ]
    return [(record["event_type"], record.get("node_id")) for record in records]


def _manifest(run: Path) -> RunManifest:
    return RunManifest.model_validate_json(
        (run / "manifests" / "run.json").read_text("utf-8")
    )


def test_import_needs_orders_mock_execution_and_persists_expanded_dependencies(
    tmp_path: Path, project: tuple[Path, Path]
) -> None:
    _run(project)
    run = _runs(tmp_path)[0]
    events = _events(run)

    for entry in ("doc.review", "doc.audit"):
        for prerequisite in ("plan", "implement"):
            assert events.index(("node_finished", prerequisite)) < events.index(
                ("node_started", entry)
            )
            assert events.index(("node_finished", prerequisite)) < events.index(
                ("invocation_started", entry)
            )
    for entry in ("doc.review", "doc.audit"):
        assert events.index(("node_finished", entry)) < events.index(
            ("node_started", "doc.summarize")
        )
    manifest = _manifest(run)
    assert manifest.status == "succeeded"
    plan = PreflightExecutionPlan.model_validate_json(
        (run / "preflight" / "execution-plan.json").read_text("utf-8")
    )
    assert plan.workflow_signature == manifest.workflow_signature
    persisted_nodes = {node.id: node.dependencies for node in plan.nodes}
    assert persisted_nodes["doc.review"] == ["doc.context", "plan", "implement"]
    assert persisted_nodes["doc.audit"] == ["plan", "implement"]
    assert persisted_nodes["doc.context"] == []
    expanded = manifest.composed_workflow["nodes"]
    assert isinstance(expanded, list)
    assert {
        node["id"]: node["needs"] for node in expanded if isinstance(node, dict)
    } == persisted_nodes


def test_import_needs_preserves_dedupe_force_and_changed_graph_identity(
    tmp_path: Path, project: tuple[Path, Path]
) -> None:
    _run(project)
    first = _runs(tmp_path)[0]
    _run(project)
    assert _runs(tmp_path) == [first]

    _run(project, force=True)
    assert len(_runs(tmp_path)) == 2
    forced = next(run for run in _runs(tmp_path) if run != first)
    assert _manifest(forced).workflow_signature == _manifest(first).workflow_signature
    assert _manifest(forced).resumed_nodes == []
    assert {
        node_id for event, node_id in _events(forced) if event == "invocation_started"
    } == set(_PROVIDER_IDS)

    previous_runs = set(_runs(tmp_path))
    write_import_needs_workflow(tmp_path, ("plan",))
    _run(project)
    changed = (set(_runs(tmp_path)) - previous_runs).pop()
    assert _manifest(changed).workflow_signature != _manifest(first).workflow_signature
    assert _manifest(changed).resumed_nodes == []


@pytest.mark.parametrize("failed_prerequisite", ["plan", "implement"])
def test_failed_import_prerequisite_blocks_all_imported_provider_branches(
    tmp_path: Path, project: tuple[Path, Path], failed_prerequisite: str
) -> None:
    (
        tmp_path / "fixtures" / failed_prerequisite / "alpha_executor_0_round1.md"
    ).unlink()

    with pytest.raises(typer.Exit) as caught:
        _run(project)

    assert caught.value.exit_code == 1
    run = _runs(tmp_path)[0]
    events = _events(run)
    assert ("node_failed", failed_prerequisite) in events
    assert _manifest(run).status == "failed"
    for node_id in ("doc.review", "doc.audit", "doc.summarize"):
        assert ("node_blocked", node_id) in events
        assert ("node_started", node_id) not in events
        assert ("invocation_started", node_id) not in events


@requires_resume_support
@pytest.mark.parametrize("unavailable", ["missing-state", "changed-artifact"])
def test_import_needs_resume_frontier_requires_all_prerequisites(
    tmp_path: Path, project: tuple[Path, Path], unavailable: str
) -> None:
    _run(project)
    run = _runs(tmp_path)[0]
    manifest = _manifest(run)
    plan = PreflightExecutionPlan.model_validate_json(
        (run / "preflight" / "execution-plan.json").read_text("utf-8")
    )
    source = find_same_context_runs(
        tmp_path / ".crewplane",
        manifest.workflow_identity,
        manifest.workflow_name,
        manifest.workflow_signature,
    )[0]
    assert set(validate_resume_frontier(source, plan).resumed_node_ids) == {
        *_PROVIDER_IDS,
        "doc.context",
    }
    if unavailable == "missing-state":
        (run / node_state_relative_path("implement")).unlink()
    else:
        (source.results_dir / "implement-result.md").write_text(
            "changed", encoding="utf-8"
        )

    frontier = validate_resume_frontier(source, plan)

    assert set(frontier.resumed_node_ids) == {"plan", "doc.context"}
