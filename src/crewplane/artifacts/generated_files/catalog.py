from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from crewplane.architecture.safe_file_reads import (
    read_contained_bytes,
)
from crewplane.architecture.safe_files import (
    contained_regular_file,
)

from ..naming import build_generated_file_result_dir_name
from . import io, snapshot_metadata
from .detection import (
    GeneratedFileLink,
    GeneratedFileReferenceDetector,
)
from .paths import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    GENERATED_FILE_SOURCE_METADATA_NAME,
    generated_file_node_prefix,
    is_reserved_workspace_path,
)
from .snapshot_policy import (
    GeneratedFileRejectionLog,
    GeneratedFileSnapshotCandidate,
    GeneratedFileSnapshotPolicy,
    GeneratedFileSnapshotSelection,
    generated_file_rejection_metadata,
    generated_file_snapshot_candidate_metadata,
    select_generated_file_snapshot_candidates,
)

MAX_GENERATED_FILE_SNAPSHOT_FILES = 100
MAX_GENERATED_FILE_SNAPSHOT_BYTES = 50 * 1024 * 1024
MAX_GENERATED_FILE_SNAPSHOT_TOTAL_BYTES = 200 * 1024 * 1024
MAX_GENERATED_FILE_SNAPSHOT_REJECTION_DETAILS = 100


@dataclass(frozen=True)
class GeneratedFileLinkResult:
    links: tuple[GeneratedFileLink, ...]
    warnings: tuple[str, ...] = ()


def build_generated_files_section(
    result_file: Path,
    workspace_root: Path,
    generated_files: Sequence[Path],
) -> str | None:
    if not generated_files:
        return None

    resolved_workspace_root = workspace_root.resolve()
    lines = ["## Generated Files", ""]
    for generated_file in generated_files:
        label = generated_file.relative_to(resolved_workspace_root).as_posix()
        link_target = os.path.relpath(generated_file, result_file.parent)
        lines.append(f"- [{label}]({_format_markdown_link_target(link_target)})")
    return "\n".join(lines) + "\n"


def build_generated_file_links_section(
    result_file: Path,
    links: Sequence[GeneratedFileLink],
) -> str | None:
    if not links:
        return None
    lines = ["## Generated Files", ""]
    seen_labels: set[str] = set()
    for link in links:
        if link.label in seen_labels:
            continue
        seen_labels.add(link.label)
        link_target = os.path.relpath(link.target_path, result_file.parent)
        lines.append(f"- [{link.label}]({_format_markdown_link_target(link_target)})")
    return "\n".join(lines) + "\n"


def generated_file_links_for_content(
    content: str,
    workspace_root: Path,
    result_file: Path,
    stage_name: str,
    materialize: bool = False,
    copy_namespace: str | None = None,
    candidate_files: Sequence[Path] | None = None,
) -> GeneratedFileLinkResult:
    resolved_candidate_files = candidate_files
    if resolved_candidate_files is None:
        resolved_candidate_files = snapshot_metadata.read_snapshot_candidate_files(
            workspace_root
        )
    detector = GeneratedFileReferenceDetector(
        workspace_root,
        source_root=snapshot_metadata.read_snapshot_source_root(workspace_root),
    )
    links: list[GeneratedFileLink] = []
    warnings: list[str] = []
    for generated_file in _ordered_generated_files_for_content(
        content,
        detector,
        workspace_root,
        resolved_candidate_files,
    ):
        relative_path = generated_file.relative_to(workspace_root.resolve()).as_posix()
        label = relative_path
        target_path = generated_file
        if materialize:
            if copy_namespace is not None:
                label = f"{copy_namespace}/{relative_path}"
            try:
                target_path = _copy_workspace_generated_file(
                    generated_file,
                    relative_path,
                    result_file,
                    stage_name,
                    copy_namespace,
                )
            except (OSError, ValueError) as exc:
                warnings.append(f"Generated-file copy failed for {label!r}: {exc}")
                continue
        links.append(GeneratedFileLink(label=label, target_path=target_path))
    return GeneratedFileLinkResult(
        links=tuple(links),
        warnings=tuple(warnings),
    )


def snapshot_generated_file_workspace(
    output_file: Path,
    workspace_root: Path,
    changed_paths: set[str] | None = None,
    candidate_files: Sequence[Path] | None = None,
    explicit_claims_only: bool = False,
    on_file_published: Callable[[Path, tuple[int, str]], None] | None = None,
    snapshot_root: Path | None = None,
) -> Path:
    content = (
        read_contained_bytes(output_file.parent, output_file.name).decode(
            "utf-8", errors="replace"
        )
        if output_file.is_file()
        else ""
    )
    selected_snapshot_root = snapshot_root or generated_file_source_root(output_file)
    resolved_workspace_root = workspace_root.resolve(strict=True)
    selection = _select_snapshot_candidates(
        content,
        resolved_workspace_root,
        changed_paths,
        candidate_files,
        explicit_claims_only,
    )
    io.generated_file_operations().reset_directory(selected_snapshot_root)
    source_metadata_signature = snapshot_metadata.write_source_metadata(
        selected_snapshot_root,
        resolved_workspace_root,
    )
    if on_file_published is not None:
        on_file_published(
            selected_snapshot_root / GENERATED_FILE_SOURCE_METADATA_NAME,
            source_metadata_signature,
        )
    copied_candidates: list[GeneratedFileSnapshotCandidate] = []
    for candidate in selection.candidates:
        if _publish_snapshot_candidate(
            candidate,
            selected_snapshot_root,
            resolved_workspace_root,
            selection.rejections,
            on_file_published,
        ):
            copied_candidates.append(candidate)
    snapshot_metadata_signature = snapshot_metadata.write_snapshot_metadata(
        selected_snapshot_root,
        [
            generated_file_snapshot_candidate_metadata(candidate)
            for candidate in copied_candidates
        ],
        selection.rejections,
    )
    if on_file_published is not None:
        on_file_published(
            selected_snapshot_root / GENERATED_FILE_SNAPSHOT_METADATA_NAME,
            snapshot_metadata_signature,
        )
    return selected_snapshot_root


def _select_snapshot_candidates(
    content: str,
    resolved_workspace_root: Path,
    changed_paths: set[str] | None,
    candidate_files: Sequence[Path] | None,
    explicit_claims_only: bool,
) -> GeneratedFileSnapshotSelection:
    detector = GeneratedFileReferenceDetector(resolved_workspace_root)
    explicit_labels = {
        path.relative_to(resolved_workspace_root).as_posix()
        for path in detector.detect_explicit_section(content)
    }
    generated_files = _ordered_generated_files_for_content(
        content,
        detector,
        resolved_workspace_root,
        candidate_files,
        explicit_claims_only,
    )
    return select_generated_file_snapshot_candidates(
        generated_files,
        GeneratedFileSnapshotPolicy(
            resolved_workspace_root=resolved_workspace_root,
            changed_paths=changed_paths,
            explicit_labels=explicit_labels,
            baseline_supplied=candidate_files is not None,
            file_count_limit=MAX_GENERATED_FILE_SNAPSHOT_FILES,
            per_file_size_limit=MAX_GENERATED_FILE_SNAPSHOT_BYTES,
            total_size_limit=MAX_GENERATED_FILE_SNAPSHOT_TOTAL_BYTES,
            rejection_detail_limit=MAX_GENERATED_FILE_SNAPSHOT_REJECTION_DETAILS,
        ),
    )


def _publish_snapshot_candidate(
    candidate: GeneratedFileSnapshotCandidate,
    snapshot_root: Path,
    resolved_workspace_root: Path,
    rejections: GeneratedFileRejectionLog,
    on_file_published: Callable[[Path, tuple[int, str]], None] | None,
) -> bool:
    target = snapshot_root.joinpath(*candidate.relative_path.parts)
    io.generated_file_operations().prepare_directory(
        snapshot_root, candidate.relative_path.parent
    )
    try:
        target_signature = io.generated_file_operations().copy_snapshot(
            candidate,
            target,
            resolved_workspace_root,
        )
    except (OSError, RuntimeError) as exc:
        rejections.record(
            generated_file_rejection_metadata(
                candidate,
                reason="copy_failed",
                error=str(exc),
            )
        )
        return False
    if on_file_published is not None:
        on_file_published(target, target_signature)
    return True


def _ordered_generated_files_for_content(
    content: str,
    detector: GeneratedFileReferenceDetector,
    workspace_root: Path,
    candidate_files: Sequence[Path] | None,
    explicit_claims_only: bool = False,
) -> tuple[Path, ...]:
    explicit_paths = detector.detect_explicit_section(content)
    if candidate_files is None:
        return () if explicit_claims_only else detector.detect(content)

    resolved_workspace_root = workspace_root.resolve(strict=True)
    candidates = _safe_unique_candidate_files(candidate_files, resolved_workspace_root)
    if not candidates:
        return ()
    if not explicit_paths:
        return () if explicit_claims_only else tuple(candidates.values())

    ordered: list[Path] = []
    seen_labels: set[str] = set()
    for explicit_path in explicit_paths:
        label = explicit_path.relative_to(resolved_workspace_root).as_posix()
        candidate = candidates.get(label)
        if candidate is None or label in seen_labels:
            continue
        ordered.append(candidate)
        seen_labels.add(label)
    if not explicit_claims_only:
        ordered.extend(
            path for label, path in candidates.items() if label not in seen_labels
        )
    return tuple(ordered)


def _safe_unique_candidate_files(
    candidate_files: Sequence[Path],
    resolved_workspace_root: Path,
) -> dict[str, Path]:
    candidates: dict[str, Path] = {}
    for candidate_file in candidate_files:
        try:
            relative_path = candidate_file.resolve(strict=False).relative_to(
                resolved_workspace_root
            )
        except (OSError, ValueError):
            continue
        if is_reserved_workspace_path(relative_path):
            continue
        contained = contained_regular_file(
            resolved_workspace_root,
            relative_path.as_posix(),
        )
        if contained is None:
            continue
        relative_label = contained.relative_to(resolved_workspace_root).as_posix()
        if is_reserved_workspace_path(Path(relative_label)):
            continue
        candidates.setdefault(relative_label, contained)
    return dict(sorted(candidates.items()))


def generated_file_source_root(output_file: Path) -> Path:
    digest = sha256(output_file.resolve(strict=False).as_posix().encode()).hexdigest()
    return (
        output_file.parent
        / "generated-file-sources"
        / f"{build_generated_file_result_dir_name(output_file.stem)}-{digest[:12]}"
    )


def _copy_workspace_generated_file(
    generated_file: Path,
    relative_path: str,
    result_file: Path,
    stage_name: str,
    copy_namespace: str | None,
) -> Path:
    target = result_file.parent / generated_file_node_prefix(stage_name)
    if copy_namespace is not None:
        target = target / build_generated_file_result_dir_name(copy_namespace)
    for part in Path(relative_path).parts:
        target = target / part
    io.generated_file_operations().copy_result(generated_file, target)
    return target


def _format_markdown_link_target(link_target: str) -> str:
    normalized_target = Path(link_target).as_posix()
    if any(char.isspace() for char in normalized_target):
        return f"<{normalized_target}>"
    return normalized_target
