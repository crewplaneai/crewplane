import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.generated_files.paths import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.review_loop.candidate_identity import (
    ProjectObservation,
    bind_candidate_identities,
    observe_project,
)
from crewplane.runtime.execution.review_loop.types import (
    ExecutorRoundArtifact,
    ExecutorRoundRequest,
)
from crewplane.runtime.execution.review_loop.validation import (
    build_executor_output_fingerprint,
)
from crewplane.runtime.workspace.invocation import invocation_slug, workspace_state_path
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_preflight import init_git_repo
from tests.helpers.workspace_records import workspace_selection_record
from tests.integration.runtime.execution.review_loop_rounds_support import (
    make_review_node,
    make_round_runtime_context,
    provider,
)
from tests.unit.runtime.workspace.state_selection_support import write_selection_state


def request_for(tmp_path: Path, managed: bool = True) -> ExecutorRoundRequest:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node()
    if managed:
        node = node.model_copy(
            update={
                "workspace_policy": workspace_selection_record(
                    enabled=True,
                    kind="worktree",
                    clean_start="strict",
                    materialization="worktree_checkout",
                )
            }
        )
    context = make_round_runtime_context()
    context.plan = context.plan.model_copy(update={"project_root": str(tmp_path)})
    executor = provider("exec", ProviderRole.EXECUTOR, "alpha")
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    return ExecutorRoundRequest(
        runtime_context=context,
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        executors=(executor,),
        artifact_dir=node_dir,
        executor_prompt="Implement.",
        previous_review_packet=None,
        previous_executor_outputs=None,
        audit_round_num=None,
        round_num=1,
    )


def artifact_for(request: ExecutorRoundRequest, content: str) -> ExecutorRoundArtifact:
    output_file = request.artifact_dir / f"alpha_round{request.round_num}.md"
    output_file.write_text(content)
    return ExecutorRoundArtifact(
        request.executors[0],
        "alpha",
        content,
        output_file,
        request.audit_round_num,
        request.round_num,
    )


def write_result(request: ExecutorRoundRequest, tree: str = "b" * 40) -> Path:
    slug = invocation_slug(
        request.node.id, "alpha", request.audit_round_num, request.round_num
    )
    path = workspace_state_path(
        request.output, request.node, slug, request.audit_round_num, request.round_num
    )
    write_selection_state(
        path, str(request.round_num) * 40, request.round_num, request.audit_round_num
    )
    payload = json.loads(path.read_text())
    payload["result"]["result_tree"] = tree
    path.write_text(json.dumps(payload))
    return path


def bind(request: ExecutorRoundRequest, content: str) -> list[ExecutorRoundArtifact]:
    return asyncio.run(
        bind_candidate_identities(
            request, [artifact_for(request, content)], ProjectObservation(None, None)
        )
    )


def test_worktree_identity_ignores_commit_and_round_metadata_but_tracks_tree(
    tmp_path: Path,
) -> None:
    request = request_for(tmp_path)
    write_result(request)
    first = bind(request, "Original implementation handoff")
    later_request = replace(request, round_num=2)
    write_result(later_request)
    unchanged = bind(later_request, "Reworded implementation handoff")
    changed_request = replace(request, round_num=3)
    write_result(changed_request, tree="c" * 40)
    changed = bind(changed_request, "Reworded implementation handoff")

    assert first[0].candidate_identity.kind == "files"
    assert build_executor_output_fingerprint(
        first
    ) == build_executor_output_fingerprint(unchanged)
    assert build_executor_output_fingerprint(
        first
    ) != build_executor_output_fingerprint(changed)
    evidence = json.loads(
        changed[0].output_file.with_suffix(".candidate.json").read_text()
    )
    assert evidence["source_fingerprint"] == "c" * 40


def test_generated_file_content_changes_identity_even_with_same_tree_and_size(
    tmp_path: Path,
) -> None:
    request = request_for(tmp_path)
    (tmp_path / "tracked.txt").write_text("tracked")
    source = init_git_repo(tmp_path)
    request.runtime_context.plan = request.runtime_context.plan.model_copy(
        update={"workspace_source": source}
    )
    write_result(request, tree=source.source_tree)
    artifact = artifact_for(request, "Implementation handoff")
    generated = tmp_path / "generated"
    generated.mkdir()
    generated_file = generated / "evidence.txt"
    generated_file.write_text("before")
    (generated / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_text(
        json.dumps(
            {"files": [{"path": "evidence.txt", "size_bytes": 6, "changed": True}]}
        )
    )
    request.runtime_context.generated_file_workspaces.record(
        request.node.id, artifact.output_file, generated
    )
    before = ProjectObservation(None, None)
    first = asyncio.run(bind_candidate_identities(request, [artifact], before))
    generated_file.write_text("after!")
    changed = asyncio.run(bind_candidate_identities(request, [artifact], before))

    assert first[0].candidate_identity.kind == "files"
    assert build_executor_output_fingerprint(
        first
    ) != build_executor_output_fingerprint(changed)


@pytest.mark.parametrize(
    "broken", ["missing_state", "malformed_state", "capture_failure", "bad_metadata"]
)
def test_unavailable_workspace_evidence_never_proves_no_progress(
    tmp_path: Path, broken: str
) -> None:
    request = request_for(tmp_path)
    artifact = artifact_for(request, "Implementation handoff")
    if broken != "missing_state":
        path = write_result(request)
        if broken == "malformed_state":
            path.write_text("{}")
    if broken == "capture_failure":
        request.runtime_context.generated_file_workspaces.record_capture_failure(
            request.node.id, artifact.output_file
        )
    if broken == "bad_metadata":
        generated = tmp_path / "generated"
        generated.mkdir()
        (generated / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_text("[]")
        request.runtime_context.generated_file_workspaces.record(
            request.node.id, artifact.output_file, generated
        )
    outputs = asyncio.run(
        bind_candidate_identities(request, [artifact], ProjectObservation(None, None))
    )

    assert build_executor_output_fingerprint(outputs) is None
    assert outputs[0].candidate_identity.kind == "unverified"


def test_project_snapshot_limit_defers_to_reviewers(tmp_path: Path) -> None:
    request = request_for(tmp_path, managed=False)
    with (tmp_path / "oversized.bin").open("wb") as handle:
        handle.truncate(129 * 1024 * 1024)

    assert asyncio.run(observe_project(request)).fingerprint is None


@pytest.mark.parametrize(
    "invalid_entry",
    [
        None,
        {"path": 1, "size_bytes": 4},
        {"path": "missing.txt", "size_bytes": 4},
        {"path": "valid.txt", "size_bytes": 99},
        {"path": "../outside.txt", "size_bytes": 4},
        {"path": "link.txt", "size_bytes": 4},
    ],
)
def test_invalid_generated_file_invalidates_entire_candidate(
    tmp_path: Path, invalid_entry: object
) -> None:
    request = request_for(tmp_path)
    artifact = artifact_for(request, "Candidate body")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "valid.txt").write_text("data", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("data", encoding="utf-8")
    (generated / "link.txt").symlink_to(outside)
    (generated / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_text(
        json.dumps({"files": [{"path": "valid.txt", "size_bytes": 4}, invalid_entry]}),
        encoding="utf-8",
    )
    request.runtime_context.generated_file_workspaces.record(
        request.node.id, artifact.output_file, generated
    )

    outputs = asyncio.run(
        bind_candidate_identities(request, [artifact], ProjectObservation(None, None))
    )

    identity = outputs[0].candidate_identity
    assert identity is not None
    assert identity.kind == "unverified"
    assert identity.reason == "generated_files_unavailable"
    assert build_executor_output_fingerprint(outputs) is None


@pytest.mark.parametrize("metadata_available", [False, True])
def test_generated_file_capture_distinguishes_empty_from_missing_metadata(
    tmp_path: Path, metadata_available: bool
) -> None:
    request = request_for(tmp_path)
    request.node = request.node.model_copy(
        update={"workspace_policy": workspace_selection_record(kind="snapshot")}
    )
    artifact = artifact_for(request, "Candidate body")
    generated = tmp_path / "generated"
    generated.mkdir()
    if metadata_available:
        (generated / GENERATED_FILE_SNAPSHOT_METADATA_NAME).write_text(
            json.dumps({"files": []}), encoding="utf-8"
        )
    request.runtime_context.generated_file_workspaces.record(
        request.node.id, artifact.output_file, generated
    )

    outputs = asyncio.run(
        bind_candidate_identities(request, [artifact], ProjectObservation(None, None))
    )

    identity = outputs[0].candidate_identity
    assert identity is not None
    assert identity.kind == ("document" if metadata_available else "unverified")
    assert (identity.fingerprint is not None) == metadata_available
