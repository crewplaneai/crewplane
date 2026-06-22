from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .generated_file_detection import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    GENERATED_FILE_SOURCE_METADATA_NAME,
    GeneratedFileLink,
    GeneratedFileReferenceDetector,
)
from .naming import build_generated_file_result_dir_name
from .safe_files import contained_regular_file

MAX_GENERATED_FILE_SNAPSHOT_FILES = 100
MAX_GENERATED_FILE_SNAPSHOT_BYTES = 50 * 1024 * 1024
MAX_GENERATED_FILE_SNAPSHOT_TOTAL_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class _GeneratedFileSnapshotCandidate:
    source_path: Path
    relative_path: Path
    relative_label: str
    size_bytes: int
    changed: bool | None


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
) -> tuple[GeneratedFileLink, ...]:
    detector = GeneratedFileReferenceDetector(
        workspace_root,
        source_root=_generated_file_snapshot_source_root(workspace_root),
    )
    links: list[GeneratedFileLink] = []
    for generated_file in detector.detect(content):
        relative_path = generated_file.relative_to(workspace_root.resolve()).as_posix()
        label = relative_path
        target_path = generated_file
        if materialize:
            target_path = _copy_workspace_generated_file(
                generated_file,
                relative_path,
                result_file,
                stage_name,
                copy_namespace,
            )
            if copy_namespace is not None:
                label = f"{copy_namespace}/{relative_path}"
        links.append(GeneratedFileLink(label=label, target_path=target_path))
    return tuple(links)


def snapshot_generated_file_workspace(
    output_file: Path,
    workspace_root: Path,
    changed_paths: set[str] | None = None,
) -> Path:
    content = output_file.read_text(encoding="utf-8")
    snapshot_root = generated_file_source_root(output_file)
    resolved_workspace_root = workspace_root.resolve(strict=True)
    detector = GeneratedFileReferenceDetector(resolved_workspace_root)
    candidates = _generated_file_snapshot_candidates(
        detector.detect(content),
        resolved_workspace_root,
        changed_paths,
    )

    _replace_generated_file_source_root(snapshot_root)
    _write_generated_file_source_metadata(snapshot_root, resolved_workspace_root)
    for candidate in candidates:
        target = snapshot_root.joinpath(*candidate.relative_path.parts)
        _ensure_contained_directory(snapshot_root, candidate.relative_path.parent)
        _copy_generated_file_snapshot_candidate(candidate, target)
    _write_generated_file_snapshot_metadata(
        snapshot_root,
        [
            _generated_file_snapshot_candidate_metadata(candidate)
            for candidate in candidates
        ],
    )
    return snapshot_root


def _generated_file_snapshot_candidates(
    generated_files: Sequence[Path],
    resolved_workspace_root: Path,
    changed_paths: set[str] | None,
) -> tuple[_GeneratedFileSnapshotCandidate, ...]:
    total_bytes = 0
    candidates: list[_GeneratedFileSnapshotCandidate] = []
    for generated_file in generated_files:
        relative_path = generated_file.relative_to(resolved_workspace_root)
        relative_label = relative_path.as_posix()
        if changed_paths is not None and relative_label not in changed_paths:
            continue
        if len(candidates) >= MAX_GENERATED_FILE_SNAPSHOT_FILES:
            raise RuntimeError("Generated-file snapshot rejected too many files.")
        size_bytes = generated_file.stat().st_size
        if size_bytes > MAX_GENERATED_FILE_SNAPSHOT_BYTES:
            raise RuntimeError(
                f"Generated-file snapshot rejected oversized file: {relative_label}"
            )
        next_total_bytes = total_bytes + size_bytes
        if next_total_bytes > MAX_GENERATED_FILE_SNAPSHOT_TOTAL_BYTES:
            raise RuntimeError("Generated-file snapshot rejected oversized total.")
        total_bytes = next_total_bytes
        candidates.append(
            _GeneratedFileSnapshotCandidate(
                source_path=generated_file,
                relative_path=relative_path,
                relative_label=relative_label,
                size_bytes=size_bytes,
                changed=(
                    relative_label in changed_paths
                    if changed_paths is not None
                    else None
                ),
            )
        )
    return tuple(candidates)


def _generated_file_snapshot_candidate_metadata(
    candidate: _GeneratedFileSnapshotCandidate,
) -> dict[str, object]:
    return {
        "path": candidate.relative_label,
        "changed": candidate.changed,
        "size_bytes": candidate.size_bytes,
    }


def _copy_generated_file_snapshot_candidate(
    candidate: _GeneratedFileSnapshotCandidate,
    target: Path,
) -> None:
    shutil.copyfile(candidate.source_path, target)
    if target.stat().st_size == candidate.size_bytes:
        return
    target.unlink(missing_ok=True)
    raise RuntimeError(
        "Generated-file snapshot source changed while copying: "
        f"{candidate.relative_label}"
    )


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
) -> None:
    metadata_file = snapshot_root / GENERATED_FILE_SNAPSHOT_METADATA_NAME
    metadata_file.write_text(
        json.dumps({"files": list(copied_files)}, sort_keys=True) + "\n",
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
    shutil.copyfile(generated_file, target)
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
        current.mkdir(exist_ok=False)
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
