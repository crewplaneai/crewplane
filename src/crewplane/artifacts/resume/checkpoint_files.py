"""Descriptor-backed checkpoint dependencies and verified copying."""

from __future__ import annotations

import hashlib
from pathlib import Path

from crewplane.architecture.contracts.artifacts import (
    build_review_audit_directory_name,
)
from crewplane.architecture.safe_files import (
    contained_regular_file,
    ensure_contained_directory,
)
from crewplane.core.file_hashing import ContentSignature, file_size_and_sha256
from crewplane.core.review_checkpoint_state import (
    CheckpointFile,
    CheckpointInvocation,
    CheckpointProgress,
)

from ..atomic import atomic_write_bytes
from ..naming import safe_artifact_name


def describe_checkpoint_file(
    root: Path,
    relative_path: str,
    invocation: CheckpointInvocation,
    purpose: str,
    signature: ContentSignature | None = None,
) -> CheckpointFile:
    path = contained_regular_file(root, relative_path)
    if path is None:
        raise ValueError(f"Checkpoint dependency is missing or unsafe: {relative_path}")
    actual = file_size_and_sha256(path)
    if signature is not None and signature != actual:
        raise ValueError(f"Checkpoint dependency changed: {relative_path}")
    return CheckpointFile.model_validate(
        {
            "task_id": invocation.task_id,
            "role": invocation.role,
            "audit": invocation.audit,
            "local_round": invocation.local_round,
            "relative_path": relative_path,
            "purpose": purpose,
            "signature": actual,
        }
    )


def read_checkpoint_file(root: Path, descriptor: CheckpointFile) -> bytes:
    path = contained_regular_file(root, descriptor.relative_path)
    if path is None:
        raise ValueError(
            f"Checkpoint dependency is missing or unsafe: {descriptor.relative_path}"
        )
    payload = path.read_bytes()
    if (len(payload), hashlib.sha256(payload).hexdigest()) != descriptor.signature or (
        contained_regular_file(root, descriptor.relative_path) != path
    ):
        raise ValueError(f"Checkpoint dependency changed: {descriptor.relative_path}")
    return payload


def copy_checkpoint_file(
    source_root: Path, target_root: Path, descriptor: CheckpointFile
) -> None:
    payload = read_checkpoint_file(source_root, descriptor)
    relative = Path(descriptor.relative_path)
    parent = ensure_contained_directory(target_root, relative.parent.as_posix())
    if descriptor.purpose == "workspace_bundle":
        parent.chmod(0o700)
    atomic_write_bytes(parent / relative.name, payload)
    read_checkpoint_file(target_root, descriptor)


def verify_checkpoint_files(root: Path, files: list[CheckpointFile]) -> None:
    for descriptor in files:
        describe_checkpoint_file(
            root,
            descriptor.relative_path,
            descriptor,
            descriptor.purpose,
            descriptor.signature,
        )


def describe_review_evidence(
    root: Path, progress: CheckpointProgress, stage_path: str, audit_rounds: int
) -> list[CheckpointFile]:
    files: dict[str, CheckpointFile] = {}
    for review in progress.reviews():
        path = Path(review.output_path)
        invocation = CheckpointInvocation(
            task_id=review.task_id,
            role=review.role,
            audit=review.audit,
            local_round=review.local_round,
        )
        for suffix in (".review.json", ".raw.txt"):
            relative = path.with_suffix(suffix).as_posix()
            files[relative] = describe_checkpoint_file(
                root, relative, invocation, "review_metadata"
            )
        relative = (
            path.parent
            / "review-state"
            / f"{safe_artifact_name(review.task_id)}-round-{review.local_round}.state.json"
        ).as_posix()
        files[relative] = describe_checkpoint_file(
            root, relative, invocation, "review_state"
        )
    for failure in progress.failures():
        directory = Path(stage_path)
        if audit_rounds > 1:
            directory /= build_review_audit_directory_name(failure.audit)
        relative = (
            directory
            / "review-state"
            / f"{safe_artifact_name(failure.task_id)}-round-{failure.local_round}.state.json"
        ).as_posix()
        files[relative] = describe_checkpoint_file(
            root, relative, failure, "review_failure"
        )
    for candidate in progress.candidates():
        relative = Path(candidate.output_path).with_suffix(".candidate.json").as_posix()
        invocation = CheckpointInvocation(
            task_id=candidate.task_id,
            role=candidate.role,
            audit=candidate.producer_audit,
            local_round=candidate.producer_round,
        )
        files[relative] = describe_checkpoint_file(
            root, relative, invocation, "candidate_identity"
        )
    return list(files.values())
