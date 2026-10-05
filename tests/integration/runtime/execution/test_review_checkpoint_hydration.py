from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from crewplane.artifacts.generated_files import catalog
from crewplane.artifacts.generated_files.catalog import (
    snapshot_generated_file_workspace,
)
from crewplane.artifacts.generated_files.paths import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    GENERATED_FILE_SOURCE_METADATA_NAME,
)
from crewplane.artifacts.generated_files.snapshot_policy import (
    GeneratedFileSnapshotCandidate,
)
from crewplane.artifacts.naming import (
    node_state_relative_path,
    review_checkpoint_relative_path,
    run_manifest_relative_path,
)
from crewplane.artifacts.resume import checkpoint_hydration
from crewplane.artifacts.resume.checkpoint_generated_files import (
    describe_generated_mapping,
)
from crewplane.artifacts.resume.checkpoint_hydration import hydrate_review_checkpoints
from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.core.review_checkpoint_state import CheckpointInvocation
from crewplane.observability.events import RuntimeLogEventPayload
from crewplane.runtime.execution.activity.telemetry import ExecutionTelemetry
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.review_loop.checkpoint import (
    restore_selected_checkpoints,
)
from crewplane.runtime.execution.sequential import execute_sequential_stage
from crewplane.runtime.execution.workflow.node import execute_node
from tests.helpers.review_checkpoints import (
    checkpoint_run,
    hydrate_checkpoint,
    observation,
    open_checkpoint,
    prepare_checkpoint_hydration,
    write_manifest,
)
from tests.helpers.workspace_preflight import init_git_repo
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    review_output,
)


async def finalized_checkpoint(root: Path, content: str = "candidate"):
    output, runtime = checkpoint_run(root)
    await execute_sequential_stage(
        runtime.plan.nodes[0],
        output,
        runtime,
        MockAgentInvoker([content, review_output(verdict="NO_FINDINGS")]),
    )
    return output, runtime


@pytest.mark.parametrize("rejection", ["file_count_limit", "copy_failed"])
@pytest.mark.parametrize("interruption", [None, "reviewers", "finalize"])
def test_partial_generated_capture_continues_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rejection: str,
    interruption: str | None,
) -> None:
    async def run():
        (tmp_path / "README.md").write_text("Generated-file capture fixture.\n")
        init_git_repo(tmp_path)
        count = 101 if rejection == "file_count_limit" else 2
        generated = [tmp_path / f"built-{index:03}.txt" for index in range(count)]
        content = "## Generated Files\n" + "\n".join(
            f"- `{path.name}`" for path in generated
        )
        original_copy = catalog.copy_generated_file_snapshot_candidate

        def reject_copy(
            candidate: GeneratedFileSnapshotCandidate, target: Path, source: Path
        ) -> tuple[int, str]:
            if candidate.relative_label == generated[-1].name:
                raise OSError("individual generated-file copy rejected")
            return original_copy(candidate, target, source)

        if rejection == "copy_failed":
            monkeypatch.setattr(
                catalog, "copy_generated_file_snapshot_candidate", reject_copy
            )
        output, runtime = checkpoint_run(tmp_path)
        publish = output.write_review_checkpoint

        def interrupt_after_publication(record):
            path = publish(record)
            if (
                isinstance(record, OpenReviewCheckpoint)
                and record.next_phase == interruption
            ):
                raise RuntimeError("interrupted after partial capture checkpoint")
            return path

        monkeypatch.setattr(
            output, "write_review_checkpoint", interrupt_after_publication
        )
        events = []

        class GeneratedFilesInvoker(MockAgentInvoker):
            async def invoke(self, *args, **kwargs):
                if not self.calls:
                    for path in generated:
                        path.write_text(path.name)
                await super().invoke(*args, **kwargs)

        invoker = GeneratedFilesInvoker([content, review_output(verdict="NO_FINDINGS")])
        execution = execute_node(
            runtime.plan.nodes[0],
            output,
            invoker,
            runtime,
            ExecutionTelemetry(
                runtime.plan.workflow_name, output.run_id, events.append
            ),
            "checkpoint.task.md",
        )
        if interruption is None:
            await execution
        else:
            with pytest.raises(RuntimeError, match="interrupted after partial capture"):
                await execution
        marker = open_checkpoint(output)
        candidate = marker.progress.candidates()[0]
        assert candidate.identity.kind == "unverified"
        assert candidate.identity.reason == "generated_files_unavailable"
        mapping = next(
            item
            for item in marker.generated_mappings
            if item.output_path == candidate.output_path
        )
        assert mapping.snapshot_path is not None
        snapshot = output.stages_dir / mapping.snapshot_path
        metadata = json.loads(
            (snapshot / GENERATED_FILE_SNAPSHOT_METADATA_NAME).read_text()
        )
        accepted = {path.name for path in generated[:-1]}
        assert {item["path"] for item in metadata["files"]} == accepted
        assert metadata["rejected_file_count"] == 1
        assert metadata["rejected_files"][0]["reason"] == rejection
        assert not (snapshot / generated[-1].name).exists()
        partial_events = [
            event
            for event in events
            if isinstance(event.payload, RuntimeLogEventPayload)
            and event.payload.operation == "artifact_capture_partial"
        ]
        assert len(partial_events) == 1
        assert partial_events[0].payload.attributes["rejected_file_count"] == 1
        source_bytes = {
            item.relative_path: (output.stages_dir / item.relative_path).read_bytes()
            for item in marker.files
        }
        if interruption is not None:
            fresh, restored = hydrate_checkpoint(output, runtime)
            assert source_bytes == {
                name: (fresh.stages_dir / name).read_bytes() for name in source_bytes
            }
            assert (
                open_checkpoint(fresh).progress.candidates()[0].identity
                == candidate.identity
            )
            restored_snapshot = restored.generated_file_workspaces.roots_for_node(
                "review"
            )[fresh.stages_dir / candidate.output_path]
            assert restored_snapshot == fresh.stages_dir / mapping.snapshot_path
            remaining = MockAgentInvoker([review_output(verdict="NO_FINDINGS")])
            await execute_node(
                restored.plan.nodes[0],
                fresh,
                remaining,
                restored,
                None,
                "checkpoint.task.md",
            )
            assert [call["role"] for call in remaining.calls] == (
                ["reviewer"] if interruption == "reviewers" else []
            )
            assert source_bytes == {
                name: (output.stages_dir / name).read_bytes() for name in source_bytes
            }
        else:
            fresh = output
            assert [call["role"] for call in invoker.calls] == ["executor", "reviewer"]
        assert (
            open_checkpoint(fresh).progress.latest_executor_outputs[0].identity
            == candidate.identity
        )
        assert (fresh.stages_dir / node_state_relative_path("review")).is_file()
        materialized = list(fresh.results_dir.rglob("built-*.txt"))
        assert {path.name for path in materialized} == accepted
        assert all(path.read_text() == path.name for path in materialized)

    asyncio.run(run())


@pytest.mark.parametrize("mapping_state", ["absent", "failed", "snapshot"])
def test_generated_mapping_tristate_and_absolute_claim_survive_finalization(
    tmp_path: Path, mapping_state: str
) -> None:
    async def run():
        project = tmp_path / "project"
        project.mkdir()
        origin = project if mapping_state != "snapshot" else tmp_path / "old-workspace"
        origin.mkdir(exist_ok=True)
        generated = origin / "built.txt"
        generated.write_text("captured bytes")
        output, runtime = await finalized_checkpoint(
            project, f"Created `{generated}`.\n"
        )
        marker = open_checkpoint(output)
        marker = marker.model_copy(
            update={
                "generated_mappings": [],
                "files": [
                    item
                    for item in marker.files
                    if item.purpose not in {"generated_file", "generated_metadata"}
                ],
            }
        )
        output.write_review_checkpoint(marker)
        candidate = marker.progress.latest_executor_outputs[0]
        snapshot = None
        if mapping_state == "snapshot":
            snapshot = snapshot_generated_file_workspace(
                output.stages_dir / candidate.output_path,
                origin,
                candidate_files=[generated],
            )
        if mapping_state != "absent":
            invocation = CheckpointInvocation(
                task_id=candidate.task_id,
                role=candidate.role,
                audit=candidate.producer_audit,
                local_round=candidate.producer_round,
            )
            mapping, files = describe_generated_mapping(
                output.stages_dir, candidate.output_path, snapshot, invocation
            )
            marker = marker.model_copy(
                update={
                    "generated_mappings": [mapping],
                    "files": [*marker.files, *files],
                }
            )
            output.write_review_checkpoint(marker)
        if mapping_state == "snapshot":
            generated.unlink()
            origin.rmdir()
        source_bytes = {
            item.relative_path: (output.stages_dir / item.relative_path).read_bytes()
            for item in marker.files
        }
        fresh, restored = hydrate_checkpoint(output, runtime)
        roots = restored.generated_file_workspaces.roots_for_node("review")
        fresh_output = fresh.stages_dir / candidate.output_path
        if mapping_state == "absent":
            assert fresh_output not in roots
        elif mapping_state == "failed":
            assert roots[fresh_output] is None
        else:
            copied = roots[fresh_output]
            assert (
                copied is not None
                and (copied / "built.txt").read_text() == "captured bytes"
            )
            assert json.loads(
                (copied / GENERATED_FILE_SOURCE_METADATA_NAME).read_text()
            )["source_root"] == str(origin)
            assert (copied / GENERATED_FILE_SNAPSHOT_METADATA_NAME).is_file()
        invoker = MockAgentInvoker([])
        await execute_node(
            restored.plan.nodes[0], fresh, invoker, restored, None, "checkpoint.task.md"
        )
        assert invoker.calls == []
        materialized = list(fresh.results_dir.rglob("built.txt"))
        assert bool(materialized) is (mapping_state == "snapshot")
        result = next(fresh.results_dir.glob("*-result.md")).read_text()
        assert ("## Generated Files" in result) is (mapping_state != "failed")
        if materialized:
            assert materialized[0].read_text() == "captured bytes"
        assert source_bytes == {
            name: (output.stages_dir / name).read_bytes() for name in source_bytes
        }
        restore_selected_checkpoints(restored, fresh)
        await execute_node(
            restored.plan.nodes[0], fresh, invoker, restored, None, "checkpoint.task.md"
        )
        assert invoker.calls == []
        assert [path.read_text() for path in materialized] == (
            ["captured bytes"] if materialized else []
        )

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["source_metadata", "snapshot_metadata", "bytes"])
@pytest.mark.parametrize("partial_capture", [False, True])
def test_generated_dependencies_must_remain_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
    partial_capture: bool,
) -> None:
    async def run():
        generated = tmp_path / "built.txt"
        generated.write_text("captured bytes")
        candidates = [generated]
        if partial_capture:
            rejected = tmp_path / "rejected.txt"
            rejected.write_text("rejected bytes")
            candidates.append(rejected)
            monkeypatch.setattr(catalog, "MAX_GENERATED_FILE_SNAPSHOT_FILES", 1)
        output, runtime = await finalized_checkpoint(tmp_path, "Created `built.txt`.\n")
        marker = open_checkpoint(output)
        marker = marker.model_copy(
            update={
                "generated_mappings": [],
                "files": [
                    item
                    for item in marker.files
                    if item.purpose not in {"generated_file", "generated_metadata"}
                ],
            }
        )
        candidate = marker.progress.latest_executor_outputs[0]
        snapshot = snapshot_generated_file_workspace(
            output.stages_dir / candidate.output_path,
            tmp_path,
            candidate_files=candidates,
        )
        invocation = CheckpointInvocation(
            task_id=candidate.task_id, role=candidate.role, audit=1, local_round=1
        )
        mapping, files = describe_generated_mapping(
            output.stages_dir, candidate.output_path, snapshot, invocation
        )
        output.write_review_checkpoint(
            marker.model_copy(
                update={
                    "generated_mappings": [mapping],
                    "files": [*marker.files, *files],
                }
            )
        )
        target = {
            "source_metadata": GENERATED_FILE_SOURCE_METADATA_NAME,
            "snapshot_metadata": GENERATED_FILE_SNAPSHOT_METADATA_NAME,
            "bytes": "built.txt",
        }[damage]
        (snapshot / target).write_text("corrupt")
        frontier = validate_resume_frontier(
            write_manifest(output, runtime, "failed"),
            runtime.plan,
            observation(runtime, output),
        )
        assert not frontier.checkpoints

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["source_mutation", "copy", "marker", "manifest"])
def test_hydration_failure_never_authorizes_runtime_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def run():
        output, runtime = await finalized_checkpoint(tmp_path)
        frontier, fresh, restored = prepare_checkpoint_hydration(output, runtime)
        original = checkpoint_hydration.copy_checkpoint_file

        def fail_copy(source, destination, descriptor):
            original(source, destination, descriptor)
            if failure == "source_mutation":
                (source / descriptor.relative_path).write_text("changed during copy")
            else:
                raise OSError("copy failed")

        def fail_publish(*args):
            del args
            raise OSError("publication failed")

        if failure in {"source_mutation", "copy"}:
            monkeypatch.setattr(checkpoint_hydration, "copy_checkpoint_file", fail_copy)
        else:
            method = (
                "write_review_checkpoint"
                if failure == "marker"
                else "record_hydrated_review_checkpoint"
            )
            monkeypatch.setattr(fresh, method, fail_publish)
        with pytest.raises((ValueError, OSError)):
            hydrate_review_checkpoints(frontier, restored.plan, fresh)
        assert not fresh.read_hydrated_review_checkpoints()
        if failure == "manifest":
            with pytest.raises(ValueError, match="no manifest hydration provenance"):
                restore_selected_checkpoints(restored, fresh)
            history = write_manifest(fresh, restored, "failed")
            assert not validate_resume_frontier(
                history, restored.plan, observation(restored, fresh)
            ).checkpoints
        assert not fresh.results_dir.exists()

    asyncio.run(run())


@pytest.mark.parametrize("point", ["setup", "entry"])
def test_project_change_after_selection_fails_before_provider_calls(
    tmp_path: Path, point: str
) -> None:
    async def run():
        output, runtime = await finalized_checkpoint(tmp_path)
        frontier, fresh, restored = prepare_checkpoint_hydration(output, runtime)
        hydrate_review_checkpoints(frontier, restored.plan, fresh)
        if point == "entry":
            restore_selected_checkpoints(restored, fresh)
        (tmp_path / "changed.txt").write_text("project changed")
        invoker = MockAgentInvoker([])
        error = ValueError if point == "setup" else NodeExecutionError
        with pytest.raises(error, match="Project contents changed"):
            if point == "setup":
                restore_selected_checkpoints(restored, fresh)
            else:
                await execute_node(
                    restored.plan.nodes[0],
                    fresh,
                    invoker,
                    restored,
                    None,
                    "checkpoint.task.md",
                )
        assert not invoker.calls

    asyncio.run(run())


@pytest.mark.parametrize("point", ["before", "during_copy"])
def test_source_marker_change_blocks_hydration_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    async def run():
        output, runtime = await finalized_checkpoint(tmp_path)
        frontier, fresh, restored = prepare_checkpoint_hydration(output, runtime)
        selected = frontier.checkpoints["review"]
        changed = selected.model_copy(update={"project_observation": None})
        copy = checkpoint_hydration.copy_checkpoint_file

        def change_marker(source, destination, descriptor):
            copy(source, destination, descriptor)
            output.write_review_checkpoint(changed)

        if point == "before":
            output.write_review_checkpoint(changed)
            message = "^Selected review checkpoint changed for node 'review'\\.$"
        else:
            monkeypatch.setattr(
                checkpoint_hydration, "copy_checkpoint_file", change_marker
            )
            message = "^Selected review checkpoint changed while copying 'review'\\.$"
        with pytest.raises(ValueError, match=message):
            hydrate_review_checkpoints(frontier, restored.plan, fresh)
        assert fresh.read_review_checkpoint("review") is None
        assert not fresh.read_hydrated_review_checkpoints()
        assert [
            (fresh.stages_dir / item.relative_path).exists() for item in selected.files
        ] == [point == "during_copy"] * len(selected.files)

    asyncio.run(run())


def test_repeated_hydration_advances_immediate_origin_and_ignores_orphans(
    tmp_path: Path,
) -> None:
    async def run():
        output, runtime = await finalized_checkpoint(tmp_path)
        (output.stages_dir / "orphan.txt").write_text("not a dependency")
        first, first_runtime = hydrate_checkpoint(output, runtime)
        second, second_runtime = hydrate_checkpoint(first, first_runtime)
        marker = open_checkpoint(second)
        assert marker.resume_origin.source_run_id == first.run_id
        assert not (second.stages_dir / "orphan.txt").exists()
        path = second.stages_dir / review_checkpoint_relative_path("review")
        assert path in second_runtime.runtime_publications.snapshot()[0]
        manifest = json.loads(
            (second.stages_dir / run_manifest_relative_path()).read_text()
        )
        assert manifest["resumed_nodes"] == []
        assert manifest["resume_source_run_id"] == first.run_id

    asyncio.run(run())


@pytest.mark.parametrize(
    "damage",
    [
        "candidate_identity",
        "raw_review",
        "normalized_review",
        "approval",
        "policy",
        "cursor",
    ],
)
def test_checkpoint_cannot_override_saved_evidence_or_finalization_policy(
    tmp_path: Path, damage: str
) -> None:
    async def run():
        output, runtime = await finalized_checkpoint(tmp_path)
        marker = open_checkpoint(output)
        progress = marker.progress.model_copy(deep=True)
        changes = {"progress": progress}
        if damage == "candidate_identity":
            progress.latest_executor_outputs[0].identity.reason = "different reason"
        elif damage == "raw_review":
            progress.latest_reviewer_outputs[0].evaluation.raw_text = "different review"
        elif damage == "normalized_review":
            progress.latest_reviewer_outputs[
                0
            ].evaluation.normalized_markdown = "different review"
        elif damage == "approval":
            progress.latest_reviewer_outputs[0].evaluation.warnings = (
                "different saved evaluation",
            )
        elif damage == "policy":
            progress.consensus_reached = False
            progress.stop_reason = "consensus_exhausted"
        else:
            changes["local_round"] = 2
        output.write_review_checkpoint(marker.model_copy(update=changes))
        frontier = validate_resume_frontier(
            write_manifest(output, runtime, "failed"),
            runtime.plan,
            observation(runtime, output),
        )
        assert not frontier.checkpoints

    asyncio.run(run())
