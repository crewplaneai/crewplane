from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from typer.testing import CliRunner

import crewplane.cli.app as cli
from crewplane.artifacts.locks import SameContextLock
from crewplane.artifacts.memory.evidence import capture_evidence
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.cli.memory.extraction import ExtractionCleanupError
from crewplane.cli.memory.review import review_candidates
from crewplane.cli.run.observability import WorkflowCancelledByUser
from crewplane.core.config import MemorySettings
from crewplane.core.memory.candidates import LessonProposal
from crewplane.version import SCHEMA_VERSION
from tests.helpers.memory import initialize_memory, lesson_document
from tests.helpers.platforms import requires_project_memory
from tests.integration.cli.workflow_runner_support import mock_runner_config

pytestmark = requires_project_memory


def learning_project(tmp_path: Path, repeat: int | None = None, failed: bool = False):
    initialize_memory(tmp_path)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "memory.md").write_text(
        "Preserve protected reads. Agent reports tests passed.",
        encoding="utf-8",
        newline="\n",
    )
    config = mock_runner_config(
        {"output_mode": "file", "output_dir": str(fixtures), "strict_file_mode": True}
    )
    config.settings.memory = MemorySettings(extractor="alpha")
    config_path = tmp_path / ".crewplane/config.yml"
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8", newline="\n"
    )
    nodes = [{"id": "memory", "mode": "parallel", "providers": ["alpha"]}]
    if failed:
        nodes.append(
            {
                "id": "fail",
                "mode": "parallel",
                "providers": ["alpha"],
                "needs": ["memory"],
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "name": "Learn",
        "memory": True,
        "nodes": nodes,
    }
    if repeat is not None:
        payload["repeat_force_run_count"] = repeat
    workflow = tmp_path / "workflow.task.md"
    workflow.write_text(
        "---\n"
        + yaml.safe_dump(payload)
        + "---\n\n## memory\n\nProtect project paths.\n"
        + ("\n## fail\n\nThis missing fixture deliberately fails.\n" if failed else ""),
        encoding="utf-8",
        newline="\n",
    )
    return fixtures, config_path, workflow


def prepare_extraction_fixture(monkeypatch, fixtures: Path, observed: list):
    original = cli.workflow_runner.execute_workflow_run

    async def run_and_prepare(*args, **kwargs):
        completed = await original(*args, **kwargs)
        observed.append(completed)
        if completed.can_learn and any(node.memory for node in completed.plan.nodes):
            packet = capture_evidence(
                completed.plan, completed.stages_dir, completed.results_dir
            )
            document = lesson_document(
                LessonProposal(
                    title="Protected paths",
                    body="Keep protected project reads.",
                    evidence_ids=(packet.evidence[0].evidence_id,),
                )
            )
            (fixtures / "extract.md").write_text(
                "```crewplane-memory-candidates\n"
                + document.model_dump_json()
                + "\n```",
                encoding="utf-8",
                newline="\n",
            )
        return completed

    monkeypatch.setattr(cli.workflow_runner, "execute_workflow_run", run_and_prepare)


def parent_manifests(root: Path):
    manifests = [
        json.loads(path.read_bytes())
        for path in (root / ".crewplane/execution-stages").glob("*/manifests/run.json")
    ]
    return [manifest for manifest in manifests if manifest["workflow_name"] == "Learn"]


def test_normal_run_learns_noninteractively_without_node_name_collision(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    fixtures, config, workflow = learning_project(tmp_path)
    completed = []
    prepare_extraction_fixture(monkeypatch, fixtures, completed)
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    )
    assert result.exit_code == 0, result.output
    assert "memory review --run" in result.output
    parent = completed[0]
    assert parent.can_learn
    assert (parent.stages_dir / "memory").is_dir()
    assert (parent.stages_dir / "manifests/memory/evidence.json").is_file()
    assert (parent.results_dir / "memory-result.md").is_file()
    snapshot = asyncio.run(MemoryStore(tmp_path).snapshot())
    assert len(snapshot.records) == 1 and snapshot.records[0].accepted_revision is None
    count = len(list((tmp_path / ".crewplane/execution-stages").iterdir()))
    duplicate = runner.invoke(
        cli.app, ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    )
    assert duplicate.exit_code == 0, duplicate.output
    assert completed[-1].kind == "dedupe"
    assert len(list((tmp_path / ".crewplane/execution-stages").iterdir())) == count


def test_repeated_passes_do_not_accept_between_runs_and_review_only_once(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    fixtures, config, workflow = learning_project(tmp_path, repeat=2)
    observed = []
    prepare_extraction_fixture(monkeypatch, fixtures, observed)
    reviews = []

    async def review(root, console, run_id=None, record_ids=None):
        del console, run_id
        snapshot = await MemoryStore(root).snapshot()
        assert all(record.accepted_revision is None for record in snapshot.records)
        reviews.append(record_ids)

    monkeypatch.setattr("crewplane.cli.run.passes.interactive", Mock(return_value=True))
    monkeypatch.setattr("crewplane.cli.run.passes.review_candidates", review)
    result = CliRunner().invoke(
        cli.app, ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    )
    assert result.exit_code == 0, result.output
    assert len(observed) == 2
    assert len(reviews) == 1
    assert len(reviews[0]) == 2
    manifests = parent_manifests(tmp_path)
    assert len(manifests) == 2
    assert manifests[0]["workflow_signature"] == manifests[1]["workflow_signature"]


@pytest.mark.parametrize("policy", ["learn_false", "memory_false", "enabled"])
@pytest.mark.parametrize("lock_released", [False, True])
def test_later_pass_release_gates_queued_review_independently_of_reloaded_policy(
    tmp_path: Path, monkeypatch, policy: str, lock_released: bool
):
    monkeypatch.chdir(tmp_path)
    fixtures, config, workflow = learning_project(tmp_path, repeat=2)
    observed = []
    prepare_extraction_fixture(monkeypatch, fixtures, observed)
    original_execute = cli.workflow_runner.execute_workflow_run
    original_release = SameContextLock.release
    held_locks = []
    release_count = 0

    async def run_and_reload_policy(*args, **kwargs):
        completed = await original_execute(*args, **kwargs)
        if len(observed) == 1:
            if policy == "learn_false":
                updated = yaml.safe_load(config.read_bytes())
                updated["settings"]["memory"]["learn"] = False
                config.write_text(
                    yaml.safe_dump(updated), encoding="utf-8", newline="\n"
                )
            elif policy == "memory_false":
                _, frontmatter, body = workflow.read_text(encoding="utf-8").split(
                    "---", 2
                )
                updated = yaml.safe_load(frontmatter)
                updated["memory"] = False
                workflow.write_text(
                    "---\n" + yaml.safe_dump(updated) + "---" + body,
                    encoding="utf-8",
                    newline="\n",
                )
        return completed

    def release(lock, terminal_complete=True, cleanup_unconfirmed=False):
        nonlocal release_count
        release_count += 1
        if release_count == 3 and not lock_released:
            assert terminal_complete and not cleanup_unconfirmed
            held_locks.append(lock)
            return False
        return original_release(lock, terminal_complete, cleanup_unconfirmed)

    review = AsyncMock(wraps=review_candidates)
    monkeypatch.setattr(
        cli.workflow_runner, "execute_workflow_run", run_and_reload_policy
    )
    monkeypatch.setattr(SameContextLock, "release", release)
    monkeypatch.setattr("crewplane.cli.run.passes.interactive", Mock(return_value=True))
    monkeypatch.setattr(
        "crewplane.cli.memory.review.interactive", Mock(return_value=True)
    )
    monkeypatch.setattr("crewplane.cli.run.passes.review_candidates", review)
    monkeypatch.setattr(
        "crewplane.cli.memory.review.typer.prompt",
        Mock(side_effect=["accept 1", "apply"]),
    )
    monkeypatch.setattr(
        "crewplane.cli.memory.review.typer.confirm", Mock(return_value=True)
    )
    try:
        result = CliRunner().invoke(
            cli.app,
            ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"],
        )
        assert result.exit_code == 0, result.output
        assert [parent.outcome for parent in observed] == ["succeeded", "succeeded"]
        assert [parent.lock_released for parent in observed] == [True, lock_released]
        assert all(parent.kind == "executed" for parent in observed)
        assert all(parent.terminal_error is None for parent in observed)
        assert review.await_count == int(lock_released)
        snapshot = asyncio.run(MemoryStore(tmp_path).snapshot())
        assert len(snapshot.records) == (
            2 if policy == "enabled" and lock_released else 1
        )
        assert sum(
            record.accepted_revision is not None for record in snapshot.records
        ) == int(lock_released)
        if not lock_released:
            assert all(
                revision.disposition == "pending"
                for record in snapshot.records
                for revision in record.revisions
            )
            assert len(held_locks) == 1 and held_locks[0].lock_dir.is_dir()
        assert all(
            manifest["status"] == "succeeded" for manifest in parent_manifests(tmp_path)
        )
    finally:
        for lock in held_locks:
            assert original_release(lock)


def test_deduped_later_pass_keeps_earlier_candidates_eligible_for_final_review(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    fixtures, config, workflow = learning_project(tmp_path, repeat=2)
    observed = []
    prepare_extraction_fixture(monkeypatch, fixtures, observed)
    original = cli.workflow_runner.execute_workflow_run

    async def run_then_reuse(*args, **kwargs):
        if observed:
            kwargs["force"] = False
        return await original(*args, **kwargs)

    review = AsyncMock()
    monkeypatch.setattr(cli.workflow_runner, "execute_workflow_run", run_then_reuse)
    monkeypatch.setattr("crewplane.cli.run.passes.interactive", Mock(return_value=True))
    monkeypatch.setattr("crewplane.cli.run.passes.review_candidates", review)
    result = CliRunner().invoke(
        cli.app, ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    )
    assert result.exit_code == 0, result.output
    assert [parent.kind for parent in observed] == ["executed", "dedupe"]
    assert observed[0].can_learn and not observed[1].can_learn
    review.assert_awaited_once()
    snapshot = asyncio.run(MemoryStore(tmp_path).snapshot())
    assert len(snapshot.records) == 1 and snapshot.records[0].accepted_revision is None
    assert review.call_args.kwargs["record_ids"] == frozenset(
        [snapshot.records[0].record_id]
    )
    assert len(parent_manifests(tmp_path)) == 1


def test_failed_parent_can_learn_earlier_completed_work_then_returns_failure(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    fixtures, config, workflow = learning_project(tmp_path, repeat=3, failed=True)
    observed = []
    prepare_extraction_fixture(monkeypatch, fixtures, observed)
    result = CliRunner().invoke(
        cli.app, ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    )
    assert result.exit_code == 1, result.output
    assert len(observed) == 1
    assert observed[0].can_learn and observed[0].outcome == "failed"
    assert parent_manifests(tmp_path)[0]["status"] == "failed"
    snapshot = asyncio.run(MemoryStore(tmp_path).snapshot())
    assert len(snapshot.records) == 1
    assert any(
        "Parent workflow outcome: failed" in value
        for value in snapshot.records[0].revisions[0].evidence[0].qualifications
    )
    packet = json.loads(
        (observed[0].stages_dir / "manifests/memory/evidence.json").read_bytes()
    )
    assert packet["parent_outcome"] == "failed"
    assert all(item["node_id"] == "memory" for item in packet["evidence"])


@pytest.mark.parametrize("failure", ["ordinary", "cleanup", "cancel"])
def test_optional_failure_isolation_and_fatal_cancellation_skip_review(
    tmp_path: Path, monkeypatch, failure
):
    monkeypatch.chdir(tmp_path)
    _fixtures, config, workflow = learning_project(tmp_path)
    error = {
        "ordinary": ValueError("invalid candidate result"),
        "cleanup": ExtractionCleanupError("unconfirmed extractor release"),
        "cancel": WorkflowCancelledByUser("extraction cancelled"),
    }[failure]
    extract = AsyncMock(side_effect=error)
    review = AsyncMock()
    monkeypatch.setattr("crewplane.cli.run.passes.extract_memory", extract)
    monkeypatch.setattr("crewplane.cli.run.passes.review_candidates", review)
    result = CliRunner().invoke(
        cli.app, ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    )
    assert result.exit_code == {"ordinary": 0, "cleanup": 1, "cancel": 130}[failure]
    assert parent_manifests(tmp_path)[0]["status"] == "succeeded"
    extract.assert_awaited_once()
    review.assert_not_called()
    assert not asyncio.run(MemoryStore(tmp_path).snapshot()).records
    if failure == "ordinary":
        assert "parent outcome is unchanged" in result.output
    if failure == "cleanup":
        assert result.exception is error
