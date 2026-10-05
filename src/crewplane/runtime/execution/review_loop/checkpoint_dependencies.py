"""Collect output, review evidence, and generated-file checkpoint dependencies."""

from __future__ import annotations

from crewplane.artifacts.resume.checkpoint_files import (
    describe_checkpoint_file,
    describe_review_evidence,
)
from crewplane.artifacts.resume.checkpoint_generated_files import (
    describe_generated_mapping,
)
from crewplane.core.review_checkpoint_state import (
    CheckpointCandidate,
    CheckpointFile,
    CheckpointGeneratedMapping,
    CheckpointInvocation,
    CheckpointProgress,
)

from .types import ReviewLoopRunContext


def collect_checkpoint_dependencies(
    context: ReviewLoopRunContext, progress: CheckpointProgress
) -> tuple[list[CheckpointFile], list[CheckpointGeneratedMapping]]:
    """Describe published outputs, review evidence, then generated dependencies.

    Output paths retain first-seen order with the last descriptor winning.
    Generated mappings follow output descriptor order and distinguish absent,
    failed, and successful captures. No files or runtime registrations change;
    missing publications, invalid evidence, and I/O errors propagate immediately.
    """
    root = context.output.stages_dir
    files = _output_dependencies(context, progress)
    files.extend(
        describe_review_evidence(
            root,
            progress,
            context.node_dir.relative_to(root).as_posix(),
            context.audit_rounds,
        )
    )
    mappings, generated_files = _generated_dependencies(context, files)
    files.extend(generated_files)
    return files, mappings


def _output_dependencies(
    context: ReviewLoopRunContext, progress: CheckpointProgress
) -> list[CheckpointFile]:
    root = context.output.stages_dir
    artifacts = progress.candidates() + progress.reviews()
    files: dict[str, CheckpointFile] = {}
    for item in artifacts:
        is_candidate = isinstance(item, CheckpointCandidate)
        invocation = CheckpointInvocation(
            task_id=item.task_id,
            role=item.role,
            audit=item.producer_audit
            if isinstance(item, CheckpointCandidate)
            else item.audit,
            local_round=item.producer_round
            if isinstance(item, CheckpointCandidate)
            else item.local_round,
        )
        path = root / item.output_path
        signature = context.runtime_context.runtime_publications.snapshot()[0].get(path)
        if signature is None:
            raise ValueError(f"Checkpoint output has no runtime publication: {path}")
        descriptor = describe_checkpoint_file(
            root,
            item.output_path,
            invocation,
            "executor_output" if is_candidate else "reviewer_output",
            signature,
        )
        files[item.output_path] = descriptor
    return list(files.values())


def _generated_dependencies(
    context: ReviewLoopRunContext, files: list[CheckpointFile]
) -> tuple[list[CheckpointGeneratedMapping], list[CheckpointFile]]:
    root = context.output.stages_dir
    roots = context.runtime_context.generated_file_workspaces.roots_for_node(
        context.stage.id
    )
    mappings: list[CheckpointGeneratedMapping] = []
    generated_files: list[CheckpointFile] = []
    for descriptor in files:
        if descriptor.purpose not in {"executor_output", "reviewer_output"}:
            continue
        path = (root / descriptor.relative_path).resolve()
        if path in roots:
            invocation = CheckpointInvocation(
                task_id=descriptor.task_id,
                role=descriptor.role,
                audit=descriptor.audit,
                local_round=descriptor.local_round,
            )
            mapping, dependencies = describe_generated_mapping(
                root, descriptor.relative_path, roots[path], invocation
            )
            mappings.append(mapping)
            generated_files.extend(dependencies)
    return mappings, generated_files
