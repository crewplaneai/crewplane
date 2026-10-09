from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.naming import run_manifest_relative_path
from crewplane.artifacts.resume import checkpoint_hydration
from crewplane.artifacts.resume.checkpoint_files import verify_checkpoint_files
from crewplane.artifacts.resume.checkpoint_hydration import (
    hydrate_review_checkpoints,
    verify_workspace_destinations,
)
from crewplane.artifacts.resume.checkpoint_store import publish_review_checkpoint
from crewplane.artifacts.resume.validation import ValidatedResumeFrontier
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.core.review_checkpoint_state import CheckpointWorkspace
from tests.helpers.platforms import symlink_or_skip
from tests.helpers.resume import make_plan, make_run_manifest
from tests.helpers.review_checkpoints import checkpoint_payload


@pytest.fixture
def workspace_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    Path,
    OpenReviewCheckpoint,
    OutputManager,
    ValidatedResumeFrontier,
    PreflightExecutionPlan,
]:
    root = tmp_path / "source"
    root.mkdir()
    checkpoint = OpenReviewCheckpoint.model_validate(checkpoint_payload())
    candidate = checkpoint.files[0]
    path = root / candidate.relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"candidate")
    candidate = candidate.model_copy(update={"signature": file_size_and_sha256(path)})
    workspaces, descriptors = [], []
    for name in ("z", "a"):
        relative = f"a/checkpoints/{name}.json"
        path = root / relative
        atomic_write_json(
            path,
            {
                "run_id": checkpoint.run_id,
                "run_key_name": checkpoint.run_key_name,
                "updated_at": "old timestamp",
                "workspace": {"retained_reason": "checkpoint", "unknown": [name]},
                "resume_origin": {"source_workspace": {"old": name}},
                "unknown": {"nested": [name, {"keep": True}]},
            },
        )
        descriptors.append(
            candidate.model_copy(
                update={
                    "relative_path": relative,
                    "purpose": "workspace_state",
                    "signature": file_size_and_sha256(path),
                }
            )
        )
        workspaces.append(
            CheckpointWorkspace(
                task_id=candidate.task_id,
                role=candidate.role,
                audit=candidate.audit,
                local_round=candidate.local_round,
                snapshot_path=relative,
                destination_path=f"a/destinations/{name}.json",
            )
        )
    checkpoint = checkpoint.model_copy(
        update={"files": [candidate, *reversed(descriptors)], "workspaces": workspaces}
    )
    publish_review_checkpoint(root, checkpoint)
    manifest = make_run_manifest(
        checkpoint.run_id, checkpoint.run_key_name, status="failed"
    )
    source = RunHistoryRecord(
        manifest, root / run_manifest_relative_path(), root, tmp_path / "results"
    )
    output = OutputManager("Checkpoint", base_dir=tmp_path / "target")
    output.write_run_manifest(make_run_manifest(output.run_id, output.run_key_name))

    def require_file_dependencies(root, plan, node, selected):
        del plan, node
        verify_checkpoint_files(root, selected.files)

    monkeypatch.setattr(
        checkpoint_hydration,
        "require_checkpoint_dependencies",
        require_file_dependencies,
    )
    frontier = ValidatedResumeFrontier(source, {}, {checkpoint.node_id: checkpoint})
    return root, checkpoint, output, frontier, make_plan(review_loop=True)


def test_workspace_rewrite_preserves_evidence_and_publication_order(
    workspace_checkpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, checkpoint, output, frontier, plan = workspace_checkpoint
    before = {
        item.relative_path: (root / item.relative_path).read_bytes()
        for item in checkpoint.files
    }
    published = []

    def capture_write(path, payload):
        published.append(path.relative_to(output.stages_dir).as_posix())
        return atomic_write_json(path, payload)

    monkeypatch.setattr(checkpoint_hydration, "atomic_write_json", capture_write)
    assert hydrate_review_checkpoints(frontier, plan, output) == (checkpoint.node_id,)
    rewritten = output.read_review_checkpoint(checkpoint.node_id)
    assert isinstance(rewritten, OpenReviewCheckpoint)
    origin = rewritten.resume_origin
    assert origin is not None
    assert published == [
        path
        for workspace in checkpoint.workspaces
        for path in (workspace.snapshot_path, workspace.destination_path)
    ]
    assert [item.relative_path for item in rewritten.files] == list(before)
    expected_files = {item.relative_path: item for item in checkpoint.files}
    for workspace in checkpoint.workspaces:
        snapshot = output.stages_dir / workspace.snapshot_path
        expected = json.loads(before[workspace.snapshot_path])
        expected.update(
            run_id=output.run_id,
            run_key_name=output.run_key_name,
            resume_origin=origin.model_dump(mode="json"),
            updated_at=origin.hydrated_at,
        )
        assert json.loads(snapshot.read_bytes()) == expected
        destination = deepcopy(expected)
        destination["workspace"]["retained_reason"] = "hydrated_resume"
        destination["resume_origin"].update(source_workspace={}, source_execution={})
        assert (
            json.loads((output.stages_dir / workspace.destination_path).read_bytes())
            == destination
        )
        expected_files[workspace.snapshot_path] = expected_files[
            workspace.snapshot_path
        ].model_copy(update={"signature": file_size_and_sha256(snapshot)})
    assert rewritten == checkpoint.model_copy(
        update={
            "run_id": output.run_id,
            "run_key_name": output.run_key_name,
            "resume_origin": origin,
            "files": list(expected_files.values()),
        }
    )
    assert verify_workspace_destinations(output.stages_dir, rewritten) == [
        output.stages_dir / item.destination_path for item in checkpoint.workspaces
    ]
    assert before == {name: (root / name).read_bytes() for name in before}
    assert not list(output.stages_dir.rglob("*.tmp"))


@pytest.mark.parametrize("failed_write", range(4))
def test_workspace_publication_failure_keeps_earlier_writes(
    workspace_checkpoint, monkeypatch: pytest.MonkeyPatch, failed_write: int
) -> None:
    root, checkpoint, output, frontier, plan = workspace_checkpoint
    before = {
        item.relative_path: (root / item.relative_path).read_bytes()
        for item in checkpoint.files
    }
    writes = [
        path
        for item in checkpoint.workspaces
        for path in (item.snapshot_path, item.destination_path)
    ]
    published = []

    def fail_write(path, payload):
        if len(published) == failed_write:
            raise OSError("workspace publication failed")
        result = atomic_write_json(path, payload)
        published.append(path.relative_to(output.stages_dir).as_posix())
        return result

    monkeypatch.setattr(checkpoint_hydration, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="^workspace publication failed$"):
        hydrate_review_checkpoints(frontier, plan, output)
    assert published == writes[:failed_write]
    for index, name in enumerate(writes):
        path = output.stages_dir / name
        if index < failed_write:
            assert json.loads(path.read_bytes())["run_id"] == output.run_id
        elif name in before:
            assert path.read_bytes() == before[name]
        else:
            assert not path.exists()
    assert before == {name: (root / name).read_bytes() for name in before}
    assert output.read_review_checkpoint(checkpoint.node_id) is None
    assert not output.read_hydrated_review_checkpoints()
    assert not list(output.stages_dir.rglob("*.tmp"))


@pytest.mark.parametrize(
    "damage", ["missing", "different", "malformed", "symlink", "hardlink"]
)
def test_workspace_verification_rejects_destination_damage(
    workspace_checkpoint, damage: str
) -> None:
    _, checkpoint, output, frontier, plan = workspace_checkpoint
    hydrate_review_checkpoints(frontier, plan, output)
    rewritten = output.read_review_checkpoint(checkpoint.node_id)
    assert isinstance(rewritten, OpenReviewCheckpoint)
    path = output.stages_dir / checkpoint.workspaces[0].destination_path
    content = path.read_bytes()
    path.unlink()
    if damage == "different":
        path.write_text("{}", encoding="utf-8", newline="\n")
    elif damage == "malformed":
        path.write_text("{", encoding="utf-8", newline="\n")
    elif damage in {"symlink", "hardlink"}:
        target = output.stages_dir / "alias.json"
        target.write_bytes(content)
        if damage == "symlink":
            symlink_or_skip(path, target)
        else:
            os.link(target, path)
    error = json.JSONDecodeError if damage == "malformed" else ValueError
    message = (
        None
        if damage == "malformed"
        else "^Hydrated workspace destination disagrees with checkpoint evidence\\.$"
    )
    with pytest.raises(error, match=message):
        verify_workspace_destinations(output.stages_dir, rewritten)


@pytest.mark.parametrize("missing_key", ["workspace", "resume_origin"])
def test_workspace_verification_preserves_missing_field_errors(
    workspace_checkpoint, missing_key: str
) -> None:
    _, checkpoint, output, frontier, plan = workspace_checkpoint
    hydrate_review_checkpoints(frontier, plan, output)
    checkpoint = output.read_review_checkpoint(checkpoint.node_id)
    assert isinstance(checkpoint, OpenReviewCheckpoint)
    workspace = checkpoint.workspaces[0]
    path = output.stages_dir / workspace.snapshot_path
    payload = json.loads(path.read_bytes())
    del payload[missing_key]
    atomic_write_json(path, payload)
    checkpoint = checkpoint.model_copy(
        update={
            "files": [
                item.model_copy(update={"signature": file_size_and_sha256(path)})
                if item.relative_path == workspace.snapshot_path
                else item
                for item in checkpoint.files
            ]
        }
    )
    with pytest.raises(KeyError) as error:
        verify_workspace_destinations(output.stages_dir, checkpoint)
    assert error.value.args == (missing_key,)
