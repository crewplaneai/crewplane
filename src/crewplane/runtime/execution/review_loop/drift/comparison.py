from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort

from ...common import ExecutionTelemetry
from ..types import ActivityWindow, DirectorySnapshot, DriftCheckResult
from .snapshots import file_snapshot_signature, manifests_dir_for

type SnapshotValue = tuple[int, str] | DirectorySnapshot


def should_check_shared_reserved_drift(
    activity_window: ActivityWindow,
    telemetry: ExecutionTelemetry | None,
    node_id: str,
) -> bool:
    if not activity_window.is_exclusive:
        return False
    if telemetry is None or telemetry.activity_tracker is None:
        return True
    current_window = telemetry.activity_tracker.snapshot(node_id)
    return (
        current_window.is_exclusive
        and current_window.version == activity_window.version
    )


def is_fatal_drift_path(
    path: Path,
    output: ArtifactStorePort,
) -> bool:
    manifests_dir = manifests_dir_for(output)
    run_log_dir = output.get_run_log_dir()
    if path.is_relative_to(output.results_dir):
        return True
    if path.is_relative_to(manifests_dir):
        return True
    return path.is_relative_to(run_log_dir)


def detect_artifact_drift(
    before_snapshot: Mapping[Path, SnapshotValue],
    after_snapshot: Mapping[Path, SnapshotValue],
    allowed_paths: set[Path],
    output: ArtifactStorePort,
    in_progress_runtime_roots: set[Path] | None = None,
    expected_runtime_publications: dict[Path, tuple[int, str]] | None = None,
) -> DriftCheckResult:
    runtime_roots = in_progress_runtime_roots or set()
    expected_publications = expected_runtime_publications or {}
    changed_paths = sorted(
        {
            *before_snapshot.keys(),
            *after_snapshot.keys(),
            *expected_publications.keys(),
        }
    )
    unexpected_paths = [
        path
        for path in changed_paths
        if (
            before_snapshot.get(path) != after_snapshot.get(path)
            or (
                path in expected_publications
                and after_snapshot.get(path) != expected_publications[path]
            )
        )
        and not (
            path in allowed_paths
            and _snapshot_change_is_allowed(
                before_snapshot.get(path),
                after_snapshot.get(path),
            )
        )
        and not (
            path in expected_publications
            and after_snapshot.get(path) == expected_publications[path]
        )
        and not any(path == root or root in path.parents for root in runtime_roots)
    ]
    if not unexpected_paths:
        return DriftCheckResult()

    fatal_paths = tuple(
        path for path in unexpected_paths if is_fatal_drift_path(path, output)
    )
    warning_paths = tuple(
        path for path in unexpected_paths if path not in set(fatal_paths)
    )
    return DriftCheckResult(
        warning_paths=warning_paths,
        fatal_paths=fatal_paths,
    )


def _snapshot_change_is_allowed(
    before: SnapshotValue | None,
    after: SnapshotValue | None,
) -> bool:
    if not isinstance(before, DirectorySnapshot) and not isinstance(
        after,
        DirectorySnapshot,
    ):
        return True
    if before is None and isinstance(after, DirectorySnapshot):
        return True
    return (
        isinstance(before, DirectorySnapshot)
        and isinstance(after, DirectorySnapshot)
        and same_directory_identity_and_access(before, after)
    )


def detect_strict_file_drift(
    file_path: Path,
    before: bytes | None,
    after: bytes | None,
) -> DriftCheckResult:
    if before == after:
        return DriftCheckResult()
    return DriftCheckResult(fatal_paths=(file_path,))


@dataclass(frozen=True, slots=True)
class _EventLogExpectation:
    expected_append: bytes
    strict_expected_append: bool
    registered_append: bytes | None
    registered_owned_append: bytes | None

    @property
    def effective_append(self) -> bytes:
        if self.registered_append is not None:
            return self.registered_append
        return self.expected_append


def detect_event_log_drift(
    event_log_path: Path,
    before: bytes | None,
    after: bytes | None,
    expected_append: bytes,
    strict_expected_append: bool,
    registered_append: bytes | None = None,
    registered_owned_append: bytes | None = None,
) -> DriftCheckResult:
    expectation = _EventLogExpectation(
        expected_append=expected_append,
        strict_expected_append=strict_expected_append,
        registered_append=registered_append,
        registered_owned_append=registered_owned_append,
    )
    if before is None:
        return _detect_new_event_log_drift(event_log_path, after, expectation)
    if after is None or len(after) < len(before) or not after.startswith(before):
        return DriftCheckResult(fatal_paths=(event_log_path,))
    if not expectation.strict_expected_append:
        return DriftCheckResult()
    return _detect_existing_event_log_drift(
        event_log_path,
        after[len(before) :],
        expectation,
    )


def _detect_new_event_log_drift(
    event_log_path: Path,
    after: bytes | None,
    expectation: _EventLogExpectation,
) -> DriftCheckResult:
    if after is None or not expectation.strict_expected_append:
        return DriftCheckResult()
    if not _registered_owned_append_contains_expected(expectation):
        return DriftCheckResult(fatal_paths=(event_log_path,))
    expected_event_append = expectation.effective_append
    if expected_event_append and event_log_append_matches_expected(
        after,
        expected_event_append,
    ):
        return DriftCheckResult()
    if after and event_log_append_matches_expected(after, expected_event_append):
        return DriftCheckResult()
    return DriftCheckResult(fatal_paths=(event_log_path,))


def _detect_existing_event_log_drift(
    event_log_path: Path,
    actual_append: bytes,
    expectation: _EventLogExpectation,
) -> DriftCheckResult:
    if not _registered_owned_append_contains_expected(expectation):
        return DriftCheckResult(fatal_paths=(event_log_path,))
    if not event_log_append_matches_expected(
        actual_append,
        expectation.effective_append,
    ):
        return DriftCheckResult(fatal_paths=(event_log_path,))
    return DriftCheckResult()


def _registered_owned_append_contains_expected(
    expectation: _EventLogExpectation,
) -> bool:
    registered_owned_append = expectation.registered_owned_append
    return registered_owned_append is None or _contains_lines(
        registered_owned_append,
        expectation.expected_append,
    )


def _contains_lines(container: bytes, required: bytes) -> bool:
    if not required:
        return True
    required_lines = required.splitlines(keepends=True)
    required_index = 0
    for line in container.splitlines(keepends=True):
        if line != required_lines[required_index]:
            continue
        required_index += 1
        if required_index == len(required_lines):
            return True
    return False


def event_log_append_matches_expected(
    actual_append: bytes,
    expected_append: bytes,
) -> bool:
    if actual_append == expected_append:
        return True

    expected_lines = expected_append.splitlines(keepends=True)
    expected_index = 0
    for line in actual_append.splitlines(keepends=True):
        if (
            expected_index < len(expected_lines)
            and line == expected_lines[expected_index]
        ):
            expected_index += 1
            continue
        if not is_ambient_runtime_warning_event(line):
            return False
    return expected_index == len(expected_lines)


def is_ambient_runtime_warning_event(line: bytes) -> bool:
    try:
        record = json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return (
        isinstance(record, dict)
        and record.get("event_type") == "runtime_log"
        and record.get("operation") == "runtime_warning"
        and "node_id" not in record
        and "provider" not in record
        and "role" not in record
        and "task_id" not in record
    )


def merge_drift_results(*results: DriftCheckResult) -> DriftCheckResult:
    warning_paths = tuple(
        sorted({path for result in results for path in result.warning_paths})
    )
    fatal_paths = tuple(
        sorted({path for result in results for path in result.fatal_paths})
    )
    return DriftCheckResult(
        warning_paths=warning_paths,
        fatal_paths=fatal_paths,
    )


def include_current_runtime_publications(
    after_snapshot: dict[Path, tuple[int, str]],
    expected_runtime_publications: dict[Path, tuple[int, str]],
) -> dict[Path, tuple[int, str]]:
    snapshot = dict(after_snapshot)
    for path, expected_signature in expected_runtime_publications.items():
        if snapshot.get(path) == expected_signature:
            continue
        try:
            current_signature = file_snapshot_signature(path)
        except OSError:
            continue
        if current_signature == expected_signature:
            snapshot[path] = current_signature
    return snapshot


def suppress_new_ancestor_directory_drift(
    directory_drift: DriftCheckResult,
    file_drift: DriftCheckResult,
    original_directories: dict[Path, DirectorySnapshot],
) -> DriftCheckResult:
    file_paths = {*file_drift.warning_paths, *file_drift.fatal_paths}

    def is_redundant(path: Path) -> bool:
        return path not in original_directories and any(
            other != path and other.is_relative_to(path) for other in file_paths
        )

    return DriftCheckResult(
        warning_paths=tuple(
            path for path in directory_drift.warning_paths if not is_redundant(path)
        ),
        fatal_paths=tuple(
            path for path in directory_drift.fatal_paths if not is_redundant(path)
        ),
    )


def allowed_directory_paths(
    allowed_paths: set[Path] | dict[Path, tuple[int, str]],
    roots: Path | tuple[Path, ...],
) -> set[Path]:
    root_paths = (roots,) if isinstance(roots, Path) else roots
    directories: set[Path] = set()
    for path in allowed_paths:
        for root in root_paths:
            if not path.is_relative_to(root):
                continue
            directories.add(root)
            current = path.parent
            while current != root:
                directories.add(current)
                current = current.parent
            break
    return directories


def same_directory_identity_and_access(
    before: DirectorySnapshot,
    after: DirectorySnapshot,
) -> bool:
    return (
        before.mode == after.mode
        and before.device == after.device
        and before.inode == after.inode
        and before.user_id == after.user_id
        and before.group_id == after.group_id
    )
