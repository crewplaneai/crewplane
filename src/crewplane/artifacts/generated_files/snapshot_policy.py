from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GeneratedFileSnapshotCandidate:
    source_path: Path
    relative_path: Path
    relative_label: str
    size_bytes: int
    source_device: int
    source_inode: int
    changed: bool | None
    discovery_source: str
    explicit: bool


@dataclass(frozen=True)
class GeneratedFileSnapshotPolicy:
    resolved_workspace_root: Path
    changed_paths: set[str] | None
    explicit_labels: set[str]
    baseline_supplied: bool
    file_count_limit: int
    per_file_size_limit: int
    total_size_limit: int
    rejection_detail_limit: int


@dataclass
class GeneratedFileRejectionLog:
    detail_limit: int
    rejected_files: list[dict[str, object]] = field(default_factory=list)
    rejected_file_count: int = 0

    def record(self, metadata: dict[str, object] | None = None) -> None:
        self.rejected_file_count += 1
        if metadata is not None and len(self.rejected_files) < self.detail_limit:
            self.rejected_files.append(metadata)

    @property
    def details_full(self) -> bool:
        return len(self.rejected_files) >= self.detail_limit

    @property
    def truncated(self) -> bool:
        return self.rejected_file_count > len(self.rejected_files)


@dataclass(frozen=True)
class GeneratedFileSnapshotSelection:
    candidates: tuple[GeneratedFileSnapshotCandidate, ...]
    rejections: GeneratedFileRejectionLog


def select_generated_file_snapshot_candidates(
    generated_files: Sequence[Path],
    policy: GeneratedFileSnapshotPolicy,
) -> GeneratedFileSnapshotSelection:
    total_bytes = 0
    candidates: list[GeneratedFileSnapshotCandidate] = []
    rejections = GeneratedFileRejectionLog(policy.rejection_detail_limit)
    for generated_file in generated_files:
        relative_path = generated_file.relative_to(policy.resolved_workspace_root)
        relative_label = relative_path.as_posix()
        if (
            policy.changed_paths is not None
            and relative_label not in policy.changed_paths
        ):
            continue
        if len(candidates) >= policy.file_count_limit and rejections.details_full:
            rejections.record()
            continue
        source_stat = generated_file.stat()
        candidate = GeneratedFileSnapshotCandidate(
            source_path=generated_file,
            relative_path=relative_path,
            relative_label=relative_label,
            size_bytes=source_stat.st_size,
            source_device=source_stat.st_dev,
            source_inode=source_stat.st_ino,
            changed=(
                relative_label in policy.changed_paths
                if policy.changed_paths is not None
                else None
            ),
            discovery_source=generated_file_discovery_source(
                relative_label,
                policy.explicit_labels,
                policy.baseline_supplied,
            ),
            explicit=relative_label in policy.explicit_labels,
        )
        if len(candidates) >= policy.file_count_limit:
            rejections.record(
                generated_file_rejection_metadata(
                    candidate,
                    reason="file_count_limit",
                    configured_limit_count=policy.file_count_limit,
                )
            )
            continue
        if candidate.size_bytes > policy.per_file_size_limit:
            rejections.record(
                generated_file_rejection_metadata(
                    candidate,
                    reason="per_file_size_limit",
                    configured_limit_bytes=policy.per_file_size_limit,
                )
            )
            continue
        next_total_bytes = total_bytes + candidate.size_bytes
        if next_total_bytes > policy.total_size_limit:
            rejections.record(
                generated_file_rejection_metadata(
                    candidate,
                    reason="total_size_limit",
                    configured_limit_bytes=policy.total_size_limit,
                )
            )
            continue
        total_bytes = next_total_bytes
        candidates.append(candidate)
    return GeneratedFileSnapshotSelection(
        candidates=tuple(candidates),
        rejections=rejections,
    )


def generated_file_snapshot_candidate_metadata(
    candidate: GeneratedFileSnapshotCandidate,
) -> dict[str, object]:
    return {
        "path": candidate.relative_label,
        "changed": candidate.changed,
        "size_bytes": candidate.size_bytes,
    }


def generated_file_rejection_metadata(
    candidate: GeneratedFileSnapshotCandidate,
    reason: str,
    configured_limit_bytes: int | None = None,
    configured_limit_count: int | None = None,
    error: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "path": candidate.relative_label,
        "size_bytes": candidate.size_bytes,
        "discovery_source": candidate.discovery_source,
        "explicit": candidate.explicit,
        "disposition": "rejected",
        "reason": reason,
    }
    if configured_limit_bytes is not None:
        metadata["configured_limit_bytes"] = configured_limit_bytes
    if configured_limit_count is not None:
        metadata["configured_limit_count"] = configured_limit_count
    if error:
        metadata["error"] = error
    return metadata


def generated_file_discovery_source(
    relative_label: str,
    explicit_labels: set[str],
    baseline_supplied: bool,
) -> str:
    if relative_label in explicit_labels:
        return "provider_explicit_section"
    if baseline_supplied:
        return "workspace_change_baseline"
    return "provider_claim"
