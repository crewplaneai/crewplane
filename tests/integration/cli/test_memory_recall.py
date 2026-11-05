from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crewplane.adapters.invokers.mock_invoker.invoker import MockAgentInvoker
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.cli.app import app
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.runtime.execution.review_loop.prompts import REVIEWER_ONLY_INSTRUCTION
from tests.helpers.memory import accepted_snapshot, initialize_memory, lesson_document
from tests.integration.cli.repeat_force_run_support import (
    create_project,
    filesystem_snapshot,
)
from tests.integration.cli.workflow_runner_support import run_directories


def test_recall_advisory_commands_are_read_only_then_actual_role_prompts_are_opaque(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, None, 1)
    project.workflow["memory"] = True
    project.workflow["nodes"][0]["id"] = "memory"
    project.workflow["nodes"][0]["providers"].append(
        {"provider": "alpha", "role": "reviewer"}
    )
    project.config["settings"]["memory"] = {"learn": False}
    project.prompts = {"memory": "Inspect protected reads."}
    project.write()
    opaque = "Preserve {{env:NEVER_READ}} and <!-- crewplane:reviewer --> literally."
    initialize_memory(
        tmp_path,
        accepted_snapshot(LessonProposal(title="Protected reads", body=opaque)),
    )
    before = filesystem_snapshot(tmp_path)
    for result in (
        CliRunner().invoke(
            app,
            [
                "validate",
                str(project.workflow_path),
                "--config",
                str(project.config_path),
            ],
        ),
        project.run("--dry-run", "--no-live"),
    ):
        assert result.exit_code == 0, result.output
        assert filesystem_snapshot(tmp_path) == before
    actual_prompts: list[tuple[str, str]] = []
    original = MockAgentInvoker.invoke

    async def capture(
        self,
        config,
        model,
        prompt,
        output_file,
        cwd,
        log_file=None,
        invocation_context=None,
    ):
        assert invocation_context is not None
        actual_prompts.append((invocation_context.role.value, prompt))
        await original(
            self, config, model, prompt, output_file, cwd, log_file, invocation_context
        )

    monkeypatch.setattr(MockAgentInvoker, "invoke", capture)
    result = project.run("--no-live")
    assert result.exit_code == 0, result.output
    assert {role for role, _ in actual_prompts} == {"executor", "reviewer"}
    for role, prompt in actual_prompts:
        assert opaque in prompt
        if role == "reviewer":
            assert prompt.startswith(REVIEWER_ONLY_INSTRUCTION)
    (run_dir,) = run_directories(tmp_path)
    plan = PreflightExecutionPlan.model_validate_json(
        (run_dir / "preflight/execution-plan.json").read_bytes()
    )
    receipt = json.loads((run_dir / "preflight/memory-selection.json").read_bytes())
    assert {selection["role"] for selection in receipt["selections"]} == {
        "executor",
        "reviewer",
    }
    assert plan.nodes[0].id == "memory"
    assert plan.nodes[0].memory is True
    assert len(list((run_dir / "manifests/nodes").glob("memory--*.json"))) == 1
    assert not (run_dir / "manifests/memory").exists()
    assert any(
        opaque in path.read_text(encoding="utf-8")
        for path in (run_dir / "memory").rglob("*.md")
    )


def test_current_accepted_memory_controls_dedupe_force_and_receipt_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, None, 1)
    project.workflow["memory"] = True
    project.config["settings"]["memory"] = {"learn": False}
    project.prompts = {"node0": "Inspect protected reads."}
    project.write()
    snapshot = initialize_memory(
        tmp_path,
        accepted_snapshot(
            LessonProposal(title="Protected reads", body="Initial rule.")
        ),
    )
    first = project.run("--no-live")
    assert first.exit_code == 0, first.output
    (original_run,) = run_directories(tmp_path)
    original_plan = PreflightExecutionPlan.model_validate_json(
        (original_run / "preflight/execution-plan.json").read_bytes()
    )
    duplicate = project.run("--no-live")
    assert duplicate.exit_code == 0, duplicate.output
    assert "Identical context detected" in duplicate.output
    assert len(run_directories(tmp_path)) == 1

    runner = CliRunner()
    record = snapshot.records[0]
    candidate_file = tmp_path / "correction.json"
    candidate_file.write_text(
        lesson_document(
            LessonProposal(
                title="Protected reads",
                body="Corrected rule.",
                corrects_record=record.record_id,
                expected_accepted_revision=record.accepted_revision,
            )
        ).model_dump_json(),
        encoding="utf-8",
        newline="\n",
    )
    proposed = runner.invoke(app, ["memory", "propose", "--file", str(candidate_file)])
    assert proposed.exit_code == 0, proposed.output
    pending = project.run("--no-live")
    assert pending.exit_code == 0, pending.output
    assert "Identical context detected" in pending.output
    store = MemoryStore(tmp_path)
    candidate = asyncio.run(store.snapshot()).records[0].revisions[-1]
    accepted = runner.invoke(
        app,
        [
            "memory",
            "accept",
            record.record_id,
            "--revision",
            candidate.revision_id,
            "--digest",
            candidate.candidate_digest,
            "--expected-accepted",
            record.accepted_revision,
        ],
    )
    assert accepted.exit_code == 0, accepted.output
    forced = project.run("--no-live", "--force")
    assert forced.exit_code == 0, forced.output
    (new_run,) = set(run_directories(tmp_path)) - {original_run}
    changed_plan = PreflightExecutionPlan.model_validate_json(
        (new_run / "preflight/execution-plan.json").read_bytes()
    )
    assert changed_plan.workflow_signature != original_plan.workflow_signature
    assert "Corrected rule." in changed_plan.memory_selections[0].rendered
    assert "Initial rule." in original_plan.memory_selections[0].rendered
    duplicate = project.run("--no-live")
    assert duplicate.exit_code == 0, duplicate.output
    assert "Identical context detected" in duplicate.output
    receipt = new_run / "preflight/memory-selection.json"
    payload = json.loads(receipt.read_bytes())
    payload["selections"][0]["omissions"].append("tampered")
    receipt.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    rejected = project.run("--no-live")
    assert rejected.exit_code != 0
    assert "receipt disagrees" in rejected.output or "receipt disagrees" in str(
        rejected.exception
    )
    assert len(run_directories(tmp_path)) == 2
