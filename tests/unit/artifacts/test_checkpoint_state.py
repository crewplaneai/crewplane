from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.atomic import json_bytes
from crewplane.artifacts.resume.checkpoint_files import describe_checkpoint_file
from crewplane.artifacts.workspace import checkpoint_state
from crewplane.artifacts.workspace.state.paths import workspace_bundle_path
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.preflight.workspace.models import (
    WorkspaceSelectionRecord,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.core.review_checkpoint_state import (
    CheckpointFile,
    CheckpointInvocation,
    CheckpointReviewerFailure,
    CheckpointWorkspace,
)
from tests.helpers.resume import make_plan
from tests.helpers.review_checkpoints import checkpoint_payload


@pytest.fixture
def workspace_checkpoint(
    tmp_path: Path,
) -> tuple[OutputManager, PreflightExecutionPlan, OpenReviewCheckpoint]:
    output = OutputManager("Workflow", base_dir=tmp_path)
    plan = make_plan(review_loop=True)
    node = plan.nodes[0]
    node.workspace_policy = WorkspaceSelectionRecord(
        enabled=True,
        logical_worktree_name="implementation",
        declaration_kind="worktree",
        materialization="worktree_checkout",
        writable=True,
        lineage_producer=True,
        setup=WorkspaceSetupRecord(
            profile_name="prepare",
            commands=[WorkspaceSetupCommandRecord(argv=["prepare"], command_index=0)],
        ),
    )
    checkpoint = OpenReviewCheckpoint.model_validate(checkpoint_payload())
    candidate = checkpoint.files[0]
    path = output.stages_dir / candidate.relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"candidate")
    checkpoint.files = [
        describe_checkpoint_file(
            output.stages_dir, candidate.relative_path, candidate, candidate.purpose
        )
    ]
    checkpoint.progress.reviewer_failures = [
        CheckpointReviewerFailure(
            task_id="beta",
            role="reviewer",
            audit=1,
            local_round=1,
            failure_kind="provider",
            warning="failed",
        )
    ]
    source: dict[str, object] = {"kind": "project", "node_id": None}
    for provider in node.provider_records:
        invocation = CheckpointInvocation(
            task_id=provider.task_id, role=provider.role, audit=1, local_round=1
        )
        destination = checkpoint_state.invocation_destination(node, invocation)
        slug = Path(destination).stem.removeprefix("workspace-state-")
        bundle = workspace_bundle_path(Path(destination), slug).as_posix()
        payload = {
            "task_id": provider.task_id,
            "provider": provider.provider,
            "role": provider.role,
            "audit_round_num": None,
            "round_num": 1,
            "source": deepcopy(source),
            "bundle": {"path": bundle},
            "setup": {
                "metadata_path": f"setup/{provider.task_id}.json",
                "log_path": f"setup/{provider.task_id}.log",
            },
        }
        snapshot = f"a/carried-{provider.task_id}.json"
        dependencies = [
            (snapshot, "workspace_state", json_bytes(payload)),
            (bundle, "workspace_bundle", b"bundle evidence"),
            (f"a/setup/{provider.task_id}.json", "workspace_setup", b"metadata"),
            (f"a/setup/{provider.task_id}.log", "workspace_setup", b"log"),
        ]
        for relative, purpose, encoded in dependencies:
            path = output.stages_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encoded)
            checkpoint.files.append(
                describe_checkpoint_file(
                    output.stages_dir, relative, invocation, purpose
                )
            )
        checkpoint.workspaces.append(
            CheckpointWorkspace(
                **invocation.model_dump(),
                snapshot_path=snapshot,
                destination_path=destination,
            )
        )
        source = {
            "kind": "candidate",
            "node_id": node.id,
            "bundle_path": bundle,
            "upstream_sources": [source],
        }
    output.write_review_checkpoint(checkpoint)
    return output, plan, checkpoint


def test_preparation_preserves_traversal_order_and_snapshot_publication(
    workspace_checkpoint: tuple[
        OutputManager, PreflightExecutionPlan, OpenReviewCheckpoint
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output, plan, checkpoint = workspace_checkpoint
    root, node = output.stages_dir, plan.nodes[0]
    original = checkpoint.model_dump()
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    reads, validations = [], []
    read = checkpoint_state.read_checkpoint_file

    def record_read(root: Path, descriptor: CheckpointFile) -> bytes:
        reads.append(descriptor.relative_path)
        return read(root, descriptor)

    def accept_payload(
        source, received_plan, received_node, payload, source_matches, run_id, run_key
    ):
        assert source.run_dir == root
        assert received_plan is plan
        assert received_node is node
        assert isinstance(source_matches, bool)
        assert (run_id, run_key) == (output.run_id, output.run_key_name)
        validations.append(payload["task_id"])
        return True

    monkeypatch.setattr(checkpoint_state, "read_checkpoint_file", record_read)
    monkeypatch.setattr(
        checkpoint_state, "checkpoint_invocation_is_valid", accept_payload
    )
    prepared = checkpoint_state.prepare_checkpoint_workspaces(
        output, plan, node, checkpoint.progress
    )

    assert reads == ["a/carried-beta.json", "a/carried-alpha.json"]
    assert validations == ["beta", "alpha"]
    assert [item.task_id for item in prepared.workspaces] == ["alpha", "beta"]
    assert [item.purpose for item in prepared.files] == [
        "workspace_state",
        "workspace_bundle",
        "workspace_setup",
        "workspace_setup",
        "workspace_state",
        "workspace_bundle",
        "workspace_setup",
        "workspace_setup",
    ]
    assert [item.task_id for item in prepared.files] == ["alpha"] * 4 + ["beta"] * 4
    assert [
        item.relative_path
        for item in prepared.files
        if item.purpose == "workspace_setup"
    ] == [
        "a/setup/alpha.json",
        "a/setup/alpha.log",
        "a/setup/beta.json",
        "a/setup/beta.log",
    ]
    assert list(prepared.snapshots) == [
        item.snapshot_path for item in prepared.workspaces
    ]
    for workspace, carried in zip(
        prepared.workspaces, checkpoint.workspaces, strict=True
    ):
        encoded = before[root / carried.snapshot_path]
        digest = hashlib.sha256(encoded).hexdigest()
        stem = Path(workspace.destination_path).stem.removeprefix("workspace-state-")
        assert workspace.snapshot_path == (
            f"a/review-state/checkpoints/workspaces/{stem}--{digest[:16]}.json"
        )
        assert prepared.snapshots[workspace.snapshot_path] == encoded
        descriptor = next(
            item
            for item in prepared.files
            if item.relative_path == workspace.snapshot_path
        )
        assert descriptor.signature == (len(encoded), digest)
    assert checkpoint.model_dump() == original
    assert {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == before

    checkpoint_state.publish_checkpoint_workspaces(root, prepared)
    identities = {
        relative: (root / relative).stat().st_ino for relative in prepared.snapshots
    }
    checkpoint_state.publish_checkpoint_workspaces(root, prepared)
    assert identities == {
        relative: (root / relative).stat().st_ino for relative in prepared.snapshots
    }
    assert all(
        (root / relative).read_bytes() == encoded
        for relative, encoded in prepared.snapshots.items()
    )
    assert not list(root.rglob("*.tmp"))


@pytest.mark.parametrize(
    "damage,error,message,expected_reads",
    [
        ("json", json.JSONDecodeError, "Expecting value", ["beta"]),
        (
            "metadata",
            ValueError,
            "Checkpoint workspace metadata must be an object",
            ["beta"],
        ),
        (
            "semantic",
            ValueError,
            "Invalid checkpoint workspace invocation",
            ["beta", "alpha"],
        ),
    ],
)
def test_preparation_stops_at_first_error_without_publishing(
    workspace_checkpoint: tuple[
        OutputManager, PreflightExecutionPlan, OpenReviewCheckpoint
    ],
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
    error: type[Exception],
    message: str,
    expected_reads: list[str],
) -> None:
    output, plan, checkpoint = workspace_checkpoint
    root = output.stages_dir
    reads = []
    read = checkpoint_state.read_checkpoint_file

    def read_payload(root: Path, descriptor: CheckpointFile) -> bytes:
        reads.append(descriptor.task_id)
        if descriptor.task_id == "beta" and damage != "semantic":
            return b"invalid JSON" if damage == "json" else b"[]"
        return read(root, descriptor)

    monkeypatch.setattr(checkpoint_state, "read_checkpoint_file", read_payload)
    monkeypatch.setattr(
        checkpoint_state, "checkpoint_invocation_is_valid", Mock(return_value=False)
    )
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(error, match=message):
        checkpoint_state.prepare_checkpoint_workspaces(
            output, plan, plan.nodes[0], checkpoint.progress
        )
    assert reads == expected_reads
    assert {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == before


@pytest.mark.parametrize(
    "damage,message,expected_reads",
    [
        ("metadata", "Invalid checkpoint workspace metadata", ["alpha"]),
        ("coordinates", "Checkpoint workspace coordinates mismatch", ["alpha"]),
        ("dependency", "Workspace dependency is not descriptor-backed", ["alpha"]),
        (
            "closure",
            "Checkpoint workspace invocation set is incomplete",
            ["alpha", "beta"],
        ),
        ("semantic", "Invalid checkpoint workspace invocation", ["alpha", "beta"]),
        (None, None, ["alpha", "beta"]),
    ],
)
def test_validation_preserves_read_and_error_order(
    workspace_checkpoint: tuple[
        OutputManager, PreflightExecutionPlan, OpenReviewCheckpoint
    ],
    monkeypatch: pytest.MonkeyPatch,
    damage: str | None,
    message: str | None,
    expected_reads: list[str],
) -> None:
    output, plan, checkpoint = workspace_checkpoint
    root, node = output.stages_dir, plan.nodes[0]
    first = checkpoint.workspaces[0]
    if damage in {"metadata", "coordinates"}:
        path = root / first.snapshot_path
        payload = json.loads(path.read_bytes())
        if damage == "coordinates":
            payload["round_num"] = 2
        else:
            payload = []
        path.write_bytes(json_bytes(payload))
        checkpoint.files = [
            describe_checkpoint_file(root, item.relative_path, item, item.purpose)
            if item.relative_path == first.snapshot_path
            else item
            for item in checkpoint.files
        ]
    elif damage == "dependency":
        checkpoint.files = [
            item
            for item in checkpoint.files
            if not item.relative_path.endswith("alpha.log")
        ]
    elif damage == "closure":
        checkpoint.progress.reviewer_failures = []
    original = checkpoint.model_dump()
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    reads, validations = [], []
    read = checkpoint_state.read_checkpoint_file

    def record_read(root: Path, descriptor: CheckpointFile) -> bytes:
        reads.append(descriptor.task_id)
        return read(root, descriptor)

    def validate_payload(*args):
        validations.append(args[3]["task_id"])
        return damage != "semantic"

    monkeypatch.setattr(checkpoint_state, "read_checkpoint_file", record_read)
    monkeypatch.setattr(
        checkpoint_state, "checkpoint_invocation_is_valid", validate_payload
    )
    if message is not None:
        with pytest.raises(ValueError, match=message):
            checkpoint_state.validate_checkpoint_workspaces(
                root, plan, node, checkpoint
            )
    else:
        paths = checkpoint_state.validate_checkpoint_workspaces(
            root, plan, node, checkpoint
        )
        assert paths == {
            item.relative_path
            for item in checkpoint.files
            if item.purpose.startswith("workspace_")
        }
    assert reads == expected_reads
    assert validations == (
        ["alpha"]
        if damage == "semantic"
        else ["alpha", "beta"]
        if damage is None
        else []
    )
    assert checkpoint.model_dump() == original
    assert {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == before
