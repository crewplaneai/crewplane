from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..naming import build_generated_file_result_dir_name
from ..safe_files import contained_regular_file
from .detection import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    GENERATED_FILE_SOURCE_METADATA_NAME,
    GeneratedFileLink,
    GeneratedFileReferenceDetector,
    is_reserved_workspace_path,
)
from .snapshot_policy import (
    GeneratedFileRejectionLog,
    GeneratedFileSnapshotCandidate,
    GeneratedFileSnapshotPolicy,
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


@dataclass(frozen=True)
class GeneratedFileSnapshotRejectionSummary:
    total_count: int = 0
    recorded_files: tuple[dict[str, object], ...] = ()

    @property
    def truncated(self) -> bool:
        return self.total_count > len(self.recorded_files)


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
        resolved_candidate_files = _generated_file_snapshot_candidate_files(
            workspace_root
        )
    detector = GeneratedFileReferenceDetector(
        workspace_root,
        source_root=_generated_file_snapshot_source_root(workspace_root),
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
            except OSError as exc:
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
    on_file_published: Callable[[Path], None] | None = None,
) -> Path:
    content = output_file.read_text(encoding="utf-8") if output_file.is_file() else ""
    snapshot_root = generated_file_source_root(output_file)
    resolved_workspace_root = workspace_root.resolve(strict=True)
    detector = GeneratedFileReferenceDetector(resolved_workspace_root)
    explicit_files = detector.detect_explicit_section(content)
    explicit_labels = {
        path.relative_to(resolved_workspace_root).as_posix() for path in explicit_files
    }
    generated_files = _ordered_generated_files_for_content(
        content,
        detector,
        resolved_workspace_root,
        candidate_files,
        explicit_claims_only,
    )
    selection = select_generated_file_snapshot_candidates(
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
    _replace_generated_file_source_root(snapshot_root)
    _write_generated_file_source_metadata(snapshot_root, resolved_workspace_root)
    if on_file_published is not None:
        on_file_published(snapshot_root / GENERATED_FILE_SOURCE_METADATA_NAME)
    copied_candidates: list[GeneratedFileSnapshotCandidate] = []
    for candidate in selection.candidates:
        target = snapshot_root.joinpath(*candidate.relative_path.parts)
        _ensure_contained_directory(snapshot_root, candidate.relative_path.parent)
        try:
            _copy_generated_file_snapshot_candidate(candidate, target)
        except (OSError, RuntimeError) as exc:
            selection.rejections.record(
                generated_file_rejection_metadata(
                    candidate,
                    reason="copy_failed",
                    error=str(exc),
                )
            )
            continue
        copied_candidates.append(candidate)
        if on_file_published is not None:
            on_file_published(target)
    _write_generated_file_snapshot_metadata(
        snapshot_root,
        [
            generated_file_snapshot_candidate_metadata(candidate)
            for candidate in copied_candidates
        ],
        selection.rejections,
    )
    if on_file_published is not None:
        on_file_published(snapshot_root / GENERATED_FILE_SNAPSHOT_METADATA_NAME)
    return snapshot_root


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


def _copy_generated_file_snapshot_candidate(
    candidate: GeneratedFileSnapshotCandidate,
    target: Path,
) -> None:
    try:
        shutil.copyfile(candidate.source_path, target)
        if target.stat().st_size == candidate.size_bytes:
            return
        raise RuntimeError(
            "Generated-file snapshot source changed while copying: "
            f"{candidate.relative_label}"
        )
    except (OSError, RuntimeError):
        target.unlink(missing_ok=True)
        raise


def generated_file_source_root(output_file: Path) -> Path:
    return _generated_file_source_root(output_file)


def _generated_file_snapshot_source_root(snapshot_root: Path) -> Path | None:
    metadata_file = contained_regular_file(
        snapshot_root,
        GENERATED_FILE_SOURCE_METADATA_NAME,
    )
    if metadata_file is None:
        return None
    try:
        payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    source_root = payload.get("source_root")
    if not isinstance(source_root, str) or not source_root:
        return None
    return Path(source_root)


def _generated_file_snapshot_candidate_files(
    snapshot_root: Path,
) -> tuple[Path, ...] | None:
    metadata_file = contained_regular_file(
        snapshot_root,
        GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    )
    if metadata_file is None:
        return None
    try:
        payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        return ()
    candidates: list[Path] = []
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            continue
        candidate = contained_regular_file(snapshot_root, raw_path)
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)


def generated_file_snapshot_rejection_summary(
    snapshot_root: Path,
) -> GeneratedFileSnapshotRejectionSummary:
    metadata_file = contained_regular_file(
        snapshot_root,
        GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    )
    if metadata_file is None:
        return GeneratedFileSnapshotRejectionSummary()
    try:
        payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return GeneratedFileSnapshotRejectionSummary()
    if not isinstance(payload, dict):
        return GeneratedFileSnapshotRejectionSummary()
    raw_rejections = payload.get("rejected_files")
    if not isinstance(raw_rejections, list):
        raw_rejections = []
    recorded_files = tuple(item for item in raw_rejections if isinstance(item, dict))
    raw_total_count = payload.get("rejected_file_count")
    total_count = (
        raw_total_count
        if isinstance(raw_total_count, int)
        and not isinstance(raw_total_count, bool)
        and raw_total_count >= len(recorded_files)
        else len(recorded_files)
    )
    return GeneratedFileSnapshotRejectionSummary(
        total_count=total_count,
        recorded_files=recorded_files,
    )


def _write_generated_file_source_metadata(
    snapshot_root: Path,
    source_root: Path,
) -> None:
    metadata_file = snapshot_root / GENERATED_FILE_SOURCE_METADATA_NAME
    metadata_file.write_text(
        json.dumps({"source_root": source_root.as_posix()}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_generated_file_snapshot_metadata(
    snapshot_root: Path,
    copied_files: Sequence[dict[str, object]],
    rejections: GeneratedFileRejectionLog,
) -> None:
    payload: dict[str, object] = {"files": list(copied_files)}
    if rejections.rejected_file_count:
        payload.update(
            {
                "rejected_file_count": rejections.rejected_file_count,
                "rejected_files": list(rejections.rejected_files),
                "rejected_files_truncated": rejections.truncated,
            }
        )
    metadata_file = snapshot_root / GENERATED_FILE_SNAPSHOT_METADATA_NAME
    metadata_file.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _copy_workspace_generated_file(
    generated_file: Path,
    relative_path: str,
    result_file: Path,
    stage_name: str,
    copy_namespace: str | None,
) -> Path:
    target = (
        result_file.parent
        / "generated-files"
        / build_generated_file_result_dir_name(stage_name)
    )
    if copy_namespace is not None:
        target = target / build_generated_file_result_dir_name(copy_namespace)
    for part in Path(relative_path).parts:
        target = target / part
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=".generated-file-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
        shutil.copyfile(generated_file, temporary_path)
        shutil.copymode(generated_file, temporary_path)
        temporary_path.replace(target)
    except OSError:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink()
        raise
    return target


def _generated_file_source_root(output_file: Path) -> Path:
    digest = sha256(output_file.resolve(strict=False).as_posix().encode()).hexdigest()
    return (
        output_file.parent
        / "generated-file-sources"
        / f"{build_generated_file_result_dir_name(output_file.stem)}-{digest[:12]}"
    )


def _replace_generated_file_source_root(path: Path) -> None:
    _ensure_safe_directory(path.parent.parent)
    _ensure_contained_directory(path.parent.parent, Path(path.parent.name))
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=False)
        return
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise RuntimeError(
            f"Generated-file source path is not a directory: {path.as_posix()}"
        )
    shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=False)


def _ensure_contained_directory(root: Path, relative_path: Path) -> Path:
    current = root
    for part in relative_path.parts:
        if part in {"", ".", ".."}:
            raise RuntimeError("Generated-file source path is unsafe.")
        current = current / part
        if current.exists() or current.is_symlink():
            _ensure_safe_directory(current)
            continue
        try:
            current.mkdir(exist_ok=False)
        except FileExistsError:
            _ensure_safe_directory(current)
    return current


def _ensure_safe_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(
            f"Generated-file source path is not a directory: {path.as_posix()}"
        )


def _format_markdown_link_target(link_target: str) -> str:
    normalized_target = Path(link_target).as_posix()
    if any(char.isspace() for char in normalized_target):
        return f"<{normalized_target}>"
    return normalized_target
