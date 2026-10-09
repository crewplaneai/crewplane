"""Descriptor-backed checkpoint dependencies and verified copying."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from crewplane.architecture.contracts.artifacts import (
    build_review_audit_directory_name,
)
from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import (
    contained_regular_file,
    ensure_contained_directory,
)
from crewplane.core.file_hashing import ContentSignature, file_size_and_sha256
from crewplane.core.review_checkpoint_state import (
    CheckpointCandidate,
    CheckpointFile,
    CheckpointInvocation,
    CheckpointProgress,
    CheckpointReview,
    CheckpointReviewerFailure,
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
    """Hash a safe dependency, then validate its descriptor metadata.

    Args:
        root: Run stage root containing the dependency.
        relative_path: Normalized relative POSIX path below the root.
        invocation: Task, role, audit, and local round to record.
        purpose: Supported checkpoint dependency purpose.
        signature: Expected size and SHA-256, or None to record the current value.

    Returns:
        A validated descriptor without modifying the file or invocation.

    Raises:
        ValueError: The file is missing, unsafe, or differs from the signature.
        pydantic.ValidationError: Descriptor metadata is invalid. File safety
            and signature checks take precedence over metadata validation.
        OSError: Inspecting or hashing the file fails.
    """
    actual = _verify_checkpoint_file_signature(root, relative_path, signature)
    return _validate_checkpoint_file_metadata(
        relative_path, invocation, purpose, actual
    )


def _verify_checkpoint_file_signature(
    root: Path, relative_path: str, signature: ContentSignature | None
) -> ContentSignature:
    path = contained_regular_file(root, relative_path)
    if path is None:
        raise ValueError(f"Checkpoint dependency is missing or unsafe: {relative_path}")
    actual = file_size_and_sha256(path)
    if signature is not None and signature != actual:
        raise ValueError(f"Checkpoint dependency changed: {relative_path}")
    return actual


def _validate_checkpoint_file_metadata(
    relative_path: str,
    invocation: CheckpointInvocation,
    purpose: str,
    signature: ContentSignature,
) -> CheckpointFile:
    return CheckpointFile.model_validate(
        {
            "task_id": invocation.task_id,
            "role": invocation.role,
            "audit": invocation.audit,
            "local_round": invocation.local_round,
            "relative_path": relative_path,
            "purpose": purpose,
            "signature": signature,
        }
    )


def read_checkpoint_file(root: Path, descriptor: CheckpointFile) -> bytes:
    """Read matching bytes and recheck the dependency's containment afterward.

    Returns:
        The complete file payload without modifying the file or descriptor.

    Raises:
        ValueError: The dependency is missing or unsafe, its bytes differ from
            the signature, or its safe resolved path changes after reading.
        OSError: Inspecting or reading the file fails.
    """
    path = contained_regular_file(root, descriptor.relative_path)
    if path is None:
        raise ValueError(
            f"Checkpoint dependency is missing or unsafe: {descriptor.relative_path}"
        )
    payload = read_contained_bytes(
        root, descriptor.relative_path, descriptor.signature[0]
    )
    if (len(payload), hashlib.sha256(payload).hexdigest()) != descriptor.signature or (
        contained_regular_file(root, descriptor.relative_path) != path
    ):
        raise ValueError(f"Checkpoint dependency changed: {descriptor.relative_path}")
    return payload


def copy_checkpoint_file(
    source_root: Path, target_root: Path, descriptor: CheckpointFile
) -> None:
    """Verify, atomically copy, and reread a dependency under the target root.

    The source is verified before creating target directories. Existing target
    files are replaced; workspace bundles set their parent directory mode to
    0700. Atomic writing owns temporary-file cleanup. Created directories and
    published files remain if a later step fails; the source stays unchanged.

    Raises:
        ValueError: Source or target verification fails, or a target directory
            is unsafe.
        OSError: File I/O, directory creation, chmod, or atomic publication fails.
    """
    payload = read_checkpoint_file(source_root, descriptor)
    relative = Path(descriptor.relative_path)
    parent = ensure_contained_directory(target_root, relative.parent.as_posix())
    if descriptor.purpose == "workspace_bundle":
        parent.chmod(0o700)
    atomic_write_bytes(parent / relative.name, payload)
    read_checkpoint_file(target_root, descriptor)


def verify_checkpoint_files(root: Path, files: list[CheckpointFile]) -> None:
    """Check dependencies in list order without modifying files or descriptors.

    Each file's safety and expected signature are checked before validating its
    descriptor metadata. Stop at the first error; an empty list succeeds.

    Raises:
        ValueError: A dependency is missing, unsafe, or has changed.
        pydantic.ValidationError: A descriptor's metadata is invalid.
        OSError: Inspecting or hashing a dependency fails.
    """
    for descriptor in files:
        relative_path = descriptor.relative_path
        purpose = descriptor.purpose
        actual = _verify_checkpoint_file_signature(
            root, relative_path, descriptor.signature
        )
        _validate_checkpoint_file_metadata(
            relative_path,
            descriptor,
            purpose,
            actual,
        )


def describe_review_evidence(
    root: Path, progress: CheckpointProgress, stage_path: str, audit_rounds: int
) -> list[CheckpointFile]:
    """Describe review, failure, and candidate evidence without modifying it.

    Traverse progress reviews, failures, then candidates in their supplied order.
    For each review, describe .review.json, .raw.txt, then review state. Failure
    state uses the stage's audit directory when audit_rounds exceeds one.
    Candidate identity uses its producer audit and round.

    Returns:
        Descriptors in first-seen path order. Later occurrences replace earlier
        descriptors for the same path without moving it or skipping verification.

    Raises:
        ValueError: An evidence file is missing or unsafe.
        pydantic.ValidationError: Invocation or descriptor metadata is invalid.
        OSError: Inspecting or hashing evidence fails. The first error propagates
            without inspecting later evidence.
    """
    files: dict[str, CheckpointFile] = {}
    for review in progress.reviews():
        for descriptor in _describe_reviewer_evidence(root, review):
            files[descriptor.relative_path] = descriptor
    for failure in progress.failures():
        descriptor = _describe_reviewer_failure(root, failure, stage_path, audit_rounds)
        files[descriptor.relative_path] = descriptor
    for candidate in progress.candidates():
        descriptor = _describe_candidate_identity(root, candidate)
        files[descriptor.relative_path] = descriptor
    return list(files.values())


def _describe_reviewer_evidence(
    root: Path, review: CheckpointReview
) -> Iterator[CheckpointFile]:
    path = Path(review.output_path)
    invocation = CheckpointInvocation(
        task_id=review.task_id,
        role=review.role,
        audit=review.audit,
        local_round=review.local_round,
    )
    for suffix in (".review.json", ".raw.txt"):
        yield describe_checkpoint_file(
            root, path.with_suffix(suffix).as_posix(), invocation, "review_metadata"
        )
    yield describe_checkpoint_file(
        root,
        _review_state_relative_path(path.parent, review),
        invocation,
        "review_state",
    )


def _describe_reviewer_failure(
    root: Path,
    failure: CheckpointReviewerFailure,
    stage_path: str,
    audit_rounds: int,
) -> CheckpointFile:
    directory = Path(stage_path)
    if audit_rounds > 1:
        directory /= build_review_audit_directory_name(failure.audit)
    return describe_checkpoint_file(
        root, _review_state_relative_path(directory, failure), failure, "review_failure"
    )


def _describe_candidate_identity(
    root: Path, candidate: CheckpointCandidate
) -> CheckpointFile:
    relative = Path(candidate.output_path).with_suffix(".candidate.json").as_posix()
    invocation = CheckpointInvocation(
        task_id=candidate.task_id,
        role=candidate.role,
        audit=candidate.producer_audit,
        local_round=candidate.producer_round,
    )
    return describe_checkpoint_file(root, relative, invocation, "candidate_identity")


def _review_state_relative_path(
    directory: Path, invocation: CheckpointInvocation
) -> str:
    return (
        directory
        / "review-state"
        / f"{safe_artifact_name(invocation.task_id)}-round-{invocation.local_round}.state.json"
    ).as_posix()
