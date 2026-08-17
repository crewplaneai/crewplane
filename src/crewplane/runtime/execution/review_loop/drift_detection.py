from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.safe_files import ensure_contained_directory
from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)

from ..common import ExecutionTelemetry
from .types import (
    ActivityWindow,
    DirectorySnapshot,
    DriftCheckResult,
    DriftGuardCallRequest,
    DriftMonitoringWindow,
    DriftRecoveryBaseline,
    EventLogAppendCapture,
    GeneratedFileDriftAllowance,
)

type SnapshotValue = tuple[int, str] | DirectorySnapshot


def file_snapshot_signature(file_path: Path) -> tuple[int, str]:
    file_stat = _single_link_regular_file_stat(file_path)
    payload = _read_single_link_regular_file(file_path, file_stat)
    return len(payload), hashlib.sha256(payload).hexdigest()


def snapshot_files(
    root: Path,
    excluded_paths: set[Path] | None = None,
    excluded_roots: set[Path] | None = None,
) -> dict[Path, tuple[int, str]]:
    excluded = excluded_paths or set()
    roots = excluded_roots or set()
    root_stat = _lstat_or_none(root)
    if root_stat is None:
        return {}
    if stat.S_ISLNK(root_stat.st_mode):
        raise RuntimeError(f"Drift snapshot root must not be a symlink: {root}")
    if stat.S_ISREG(root_stat.st_mode):
        if _snapshot_path_is_excluded(root, excluded, roots):
            return {}
        try:
            return {root: _snapshot_signature(root, root_stat)}
        except FileNotFoundError:
            return {}
    if not stat.S_ISDIR(root_stat.st_mode):
        return {}

    snapshot: dict[Path, tuple[int, str]] = {}
    for file_path in root.rglob("*"):
        if _snapshot_path_is_excluded(file_path, excluded, roots):
            continue
        path_stat = _lstat_or_none(file_path)
        if path_stat is None or stat.S_ISDIR(path_stat.st_mode):
            continue
        try:
            snapshot[file_path] = _snapshot_signature(file_path, path_stat)
        except FileNotFoundError:
            continue
    return snapshot


def snapshot_file_bytes(
    root: Path,
    excluded_paths: set[Path] | None = None,
    excluded_roots: set[Path] | None = None,
) -> dict[Path, bytes]:
    contents: dict[Path, bytes] = {}
    for path in snapshot_files(
        root,
        excluded_paths=excluded_paths,
        excluded_roots=excluded_roots,
    ):
        path_stat = _lstat_or_none(path)
        if path_stat is None or not _is_single_link_regular_file(path_stat):
            continue
        try:
            contents[path] = _read_single_link_regular_file(path, path_stat)
        except FileNotFoundError:
            continue
    return contents


def snapshot_directories(
    root: Path,
    excluded_paths: set[Path] | None = None,
    excluded_roots: set[Path] | None = None,
) -> dict[Path, DirectorySnapshot]:
    """Record real directory entries so file/directory substitutions are visible."""

    excluded = excluded_paths or set()
    roots = excluded_roots or set()
    root_stat = _lstat_or_none(root)
    if root_stat is None:
        return {}
    if stat.S_ISLNK(root_stat.st_mode):
        raise RuntimeError(f"Drift snapshot root must not be a symlink: {root}")
    if not stat.S_ISDIR(root_stat.st_mode):
        return {}
    snapshot = {root: _directory_snapshot(root, root_stat)}
    for directory_path in root.rglob("*"):
        if _snapshot_path_is_excluded(directory_path, excluded, roots):
            continue
        path_stat = _lstat_or_none(directory_path)
        if path_stat is None or not stat.S_ISDIR(path_stat.st_mode):
            continue
        try:
            snapshot[directory_path] = _directory_snapshot(
                directory_path,
                path_stat,
            )
        except FileNotFoundError:
            continue
    return snapshot


def _snapshot_path_is_excluded(
    path: Path,
    excluded_paths: set[Path],
    excluded_roots: set[Path],
) -> bool:
    return path in excluded_paths or _snapshot_path_is_within_roots(
        path,
        excluded_roots,
    )


def _snapshot_path_is_within_roots(path: Path, roots: set[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def signatures_for_bytes(
    content_by_path: dict[Path, bytes],
) -> dict[Path, tuple[int, str]]:
    return {
        path: (len(content), hashlib.sha256(content).hexdigest())
        for path, content in content_by_path.items()
    }


def capture_activity_window(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
) -> ActivityWindow:
    if telemetry is None or telemetry.activity_tracker is None:
        return ActivityWindow(is_exclusive=True, version=None)
    snapshot = telemetry.activity_tracker.snapshot(node_id)
    return ActivityWindow(
        is_exclusive=snapshot.is_exclusive,
        version=snapshot.version,
    )


def read_file_bytes(file_path: Path) -> bytes | None:
    file_stat = _lstat_or_none(file_path)
    if file_stat is None:
        return None
    return _read_single_link_regular_file(file_path, file_stat)


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_single_link_regular_file(file_stat: os.stat_result) -> bool:
    return stat.S_ISREG(file_stat.st_mode) and file_stat.st_nlink == 1


def _single_link_regular_file_stat(path: Path) -> os.stat_result:
    file_stat = _lstat_or_none(path)
    if file_stat is None:
        raise FileNotFoundError(path)
    if not _is_single_link_regular_file(file_stat):
        raise ValueError(f"Drift snapshots require single-link regular files: {path}")
    return file_stat


def _read_single_link_regular_file(
    path: Path,
    file_stat: os.stat_result,
) -> bytes:
    if not _is_single_link_regular_file(file_stat):
        raise RuntimeError(f"Drift snapshot path is not a safe regular file: {path}")
    payload = path.read_bytes()
    current_stat = _single_link_regular_file_stat(path)
    if (
        current_stat.st_dev != file_stat.st_dev
        or current_stat.st_ino != file_stat.st_ino
        or current_stat.st_size != file_stat.st_size
    ):
        raise RuntimeError(f"Drift snapshot path changed during read: {path}")
    return payload


def _snapshot_signature(
    path: Path,
    path_stat: os.stat_result,
) -> tuple[int, str]:
    if _is_single_link_regular_file(path_stat):
        return file_snapshot_signature(path)
    target = os.readlink(path) if stat.S_ISLNK(path_stat.st_mode) else ""
    descriptor = ":".join(
        str(value)
        for value in (
            path_stat.st_mode,
            path_stat.st_dev,
            path_stat.st_ino,
            path_stat.st_nlink,
            path_stat.st_size,
            path_stat.st_mtime_ns,
            target,
        )
    ).encode("utf-8")
    return len(descriptor), hashlib.sha256(descriptor).hexdigest()


def _directory_snapshot(
    path: Path,
    path_stat: os.stat_result,
) -> DirectorySnapshot:
    with os.scandir(path) as entries:
        entry_names = tuple(sorted(entry.name for entry in entries))
    return DirectorySnapshot(
        mode=path_stat.st_mode,
        device=path_stat.st_dev,
        inode=path_stat.st_ino,
        user_id=path_stat.st_uid,
        group_id=path_stat.st_gid,
        link_count=path_stat.st_nlink,
        changed_at_ns=path_stat.st_ctime_ns,
        entry_names=entry_names,
    )


def shared_reserved_snapshot(
    node_dir: Path,
    output: ArtifactStorePort,
) -> dict[Path, tuple[int, str]]:
    del node_dir
    manifests_dir = _manifests_dir(output)
    run_log_dir = output.get_run_log_dir()
    excluded_run_log_paths = {
        output.get_run_event_log_path(),
        output.get_run_summary_path(),
    }
    snapshot: dict[Path, tuple[int, str]] = {}
    for root in (output.results_dir, manifests_dir):
        snapshot.update(snapshot_files(root))
    snapshot.update(snapshot_files(run_log_dir, excluded_paths=excluded_run_log_paths))
    return snapshot


def shared_reserved_original_bytes(
    node_dir: Path,
    output: ArtifactStorePort,
) -> dict[Path, bytes]:
    del node_dir
    manifests_dir = _manifests_dir(output)
    run_log_dir = output.get_run_log_dir()
    excluded_run_log_paths = {
        output.get_run_event_log_path(),
        output.get_run_summary_path(),
    }
    original_bytes: dict[Path, bytes] = {}
    for root in (output.results_dir, manifests_dir):
        original_bytes.update(snapshot_file_bytes(root))
    original_bytes.update(
        snapshot_file_bytes(run_log_dir, excluded_paths=excluded_run_log_paths)
    )
    return original_bytes


def capture_drift_monitoring_window(
    node_id: str,
    node_dir: Path,
    output: ArtifactStorePort,
    telemetry: ExecutionTelemetry | None,
    runtime_publications: RuntimePublicationRegistry | None = None,
    runtime_owned_paths: set[Path] | None = None,
    runtime_owned_roots: set[Path] | None = None,
    recovery_baseline: DriftRecoveryBaseline | None = None,
) -> DriftMonitoringWindow:
    if runtime_publications is None:
        return _capture_drift_monitoring_window_once(
            node_id,
            node_dir,
            output,
            telemetry,
            None,
            runtime_owned_paths,
            runtime_owned_roots,
            recovery_baseline,
        )
    while True:
        _, before_version = runtime_publications.snapshot()
        try:
            monitoring_window = _capture_drift_monitoring_window_once(
                node_id,
                node_dir,
                output,
                telemetry,
                runtime_publications,
                runtime_owned_paths,
                runtime_owned_roots,
                recovery_baseline,
            )
        except RuntimeError:
            _, after_version = runtime_publications.snapshot()
            if after_version == before_version:
                raise
            continue
        _, after_version = runtime_publications.snapshot()
        if after_version == before_version:
            return monitoring_window


def _capture_drift_monitoring_window_once(
    node_id: str,
    node_dir: Path,
    output: ArtifactStorePort,
    telemetry: ExecutionTelemetry | None,
    runtime_publications: RuntimePublicationRegistry | None,
    runtime_owned_paths: set[Path] | None,
    runtime_owned_roots: set[Path] | None,
    recovery_baseline: DriftRecoveryBaseline | None,
) -> DriftMonitoringWindow:
    activity_window = capture_activity_window(telemetry, node_id)
    baseline = recovery_baseline or _capture_drift_recovery_baseline_once(
        node_dir,
        output,
        runtime_publications,
        runtime_owned_paths,
        runtime_owned_roots,
    )
    if runtime_publications is None:
        event_log_before = read_file_bytes(output.get_run_event_log_path())
        event_publication_cursor = None
    else:
        with runtime_publications.event_observation():
            event_log_before = read_file_bytes(output.get_run_event_log_path())
            event_publication_cursor = runtime_publications.event_publication_cursor()
    return DriftMonitoringWindow(
        node_snapshot=baseline.node_snapshot,
        shared_reserved_snapshot=baseline.shared_reserved_snapshot,
        summary_before=read_file_bytes(output.get_run_summary_path()),
        event_log_before=event_log_before,
        activity_window=activity_window,
        node_original_bytes=baseline.node_original_bytes,
        shared_reserved_original_bytes=baseline.shared_reserved_original_bytes,
        node_directory_snapshot=baseline.node_directory_snapshot,
        shared_reserved_directory_snapshot=baseline.shared_reserved_directory_snapshot,
        node_original_directories=set(baseline.node_directory_snapshot),
        shared_reserved_original_directories=set(
            baseline.shared_reserved_directory_snapshot
        ),
        event_publication_cursor=event_publication_cursor,
    )


def capture_drift_recovery_baseline(
    node_dir: Path,
    output: ArtifactStorePort,
    runtime_publications: RuntimePublicationRegistry | None = None,
    runtime_owned_paths: set[Path] | None = None,
    runtime_owned_roots: set[Path] | None = None,
) -> DriftRecoveryBaseline:
    if runtime_publications is None:
        return _capture_drift_recovery_baseline_once(
            node_dir,
            output,
            None,
            runtime_owned_paths,
            runtime_owned_roots,
        )
    while True:
        _, before_version = runtime_publications.snapshot()
        try:
            baseline = _capture_drift_recovery_baseline_once(
                node_dir,
                output,
                runtime_publications,
                runtime_owned_paths,
                runtime_owned_roots,
            )
        except (OSError, RuntimeError, ValueError):
            _, after_version = runtime_publications.snapshot()
            if after_version == before_version:
                raise
            continue
        _, after_version = runtime_publications.snapshot()
        if after_version == before_version:
            return baseline


def _capture_drift_recovery_baseline_once(
    node_dir: Path,
    output: ArtifactStorePort,
    runtime_publications: RuntimePublicationRegistry | None,
    runtime_owned_paths: set[Path] | None,
    runtime_owned_roots: set[Path] | None,
) -> DriftRecoveryBaseline:
    owned_paths = runtime_owned_paths or set()
    owned_roots = runtime_owned_roots or set()
    if runtime_publications is None:
        node_original_bytes = snapshot_file_bytes(
            node_dir,
            excluded_paths=owned_paths,
            excluded_roots=owned_roots,
        )
        shared_original_bytes = shared_reserved_original_bytes(node_dir, output)
        node_snapshot = signatures_for_bytes(node_original_bytes)
        shared_snapshot = signatures_for_bytes(shared_original_bytes)
    else:
        node_snapshot = snapshot_files(
            node_dir,
            excluded_paths=owned_paths,
            excluded_roots=owned_roots,
        )
        shared_snapshot = shared_reserved_snapshot(node_dir, output)
        _capture_recovery_snapshots(runtime_publications, node_snapshot)
        _capture_recovery_snapshots(runtime_publications, shared_snapshot)
        node_original_bytes = {}
        shared_original_bytes = {}
    node_directories = snapshot_directories(
        node_dir,
        excluded_paths=owned_paths,
        excluded_roots=owned_roots,
    )
    shared_directories = _shared_reserved_directory_snapshot(node_dir, output)
    return DriftRecoveryBaseline(
        node_snapshot=node_snapshot,
        shared_reserved_snapshot=shared_snapshot,
        node_original_bytes=node_original_bytes,
        shared_reserved_original_bytes=shared_original_bytes,
        node_directory_snapshot=node_directories,
        shared_reserved_directory_snapshot=shared_directories,
    )


def _capture_recovery_snapshots(
    publications: RuntimePublicationRegistry,
    signatures: dict[Path, tuple[int, str]],
) -> None:
    for path, signature in signatures.items():
        path_stat = _lstat_or_none(path)
        if path_stat is None or not _is_single_link_regular_file(path_stat):
            continue
        publications.capture_recovery_snapshot(path, signature)


def _shared_reserved_directory_snapshot(
    node_dir: Path,
    output: ArtifactStorePort,
) -> dict[Path, DirectorySnapshot]:
    del node_dir
    manifests_dir = _manifests_dir(output)
    run_log_dir = output.get_run_log_dir()
    excluded_paths = {
        output.get_run_event_log_path(),
        output.get_run_summary_path(),
    }
    snapshot: dict[Path, DirectorySnapshot] = {}
    for root in (output.results_dir, manifests_dir):
        snapshot.update(snapshot_directories(root))
    snapshot.update(snapshot_directories(run_log_dir, excluded_paths))
    return snapshot


def restore_fatal_artifacts(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    fatal_paths: tuple[Path, ...],
) -> None:
    if not fatal_paths:
        return
    publications = request.runtime_publications
    if publications is None and request.drift_session is not None:
        publications = request.drift_session.runtime_publications
    if publications is None:
        publications = request.runtime_context.runtime_publications
    with publications.transaction():
        _restore_fatal_artifacts(
            request,
            monitoring_window,
            fatal_paths,
            publications,
        )


def _restore_fatal_artifacts(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    fatal_paths: tuple[Path, ...],
    publications: RuntimePublicationRegistry | None,
) -> None:
    originals = {
        **monitoring_window.node_original_bytes,
        **monitoring_window.shared_reserved_original_bytes,
    }
    original_directories = {
        *monitoring_window.node_original_directories,
        *monitoring_window.shared_reserved_original_directories,
    }
    original_directory_snapshots = {
        **monitoring_window.node_directory_snapshot,
        **(monitoring_window.shared_reserved_directory_snapshot or {}),
    }
    summary_path = request.output.get_run_summary_path()
    event_log_path = request.output.get_run_event_log_path()
    if monitoring_window.summary_before is not None:
        originals[summary_path] = monitoring_window.summary_before
    event_log_recovery = (
        _event_log_recovery_payload(monitoring_window, publications)
        if event_log_path in fatal_paths
        else None
    )

    restore_roots = (
        request.node_dir,
        request.output.results_dir,
        _manifests_dir(request.output),
        request.output.get_run_log_dir(),
    )
    restored_files: set[Path] = set()
    for path in sorted(fatal_paths, key=lambda item: (len(item.parts), item)):
        if any(parent in restored_files for parent in path.parents):
            continue
        _ensure_restore_parent(path, restore_roots)
        if path in original_directories:
            _restore_directory(path, original_directory_snapshots[path])
            continue
        if path == event_log_path:
            original = event_log_recovery
        else:
            if publications is not None and _atomic_restore_registered(
                path,
                publications,
            ):
                restored_files.add(path)
                continue
            original = originals.get(path)
        if original is None:
            _remove_existing_path(path)
            continue
        _remove_existing_directory(path)
        _atomic_restore(path, original)
        restored_files.add(path)


def _event_log_recovery_payload(
    monitoring_window: DriftMonitoringWindow,
    publications: RuntimePublicationRegistry | None,
) -> bytes | None:
    baseline = monitoring_window.event_log_before
    cursor = monitoring_window.event_publication_cursor
    if publications is None or cursor is None:
        return baseline
    registered_append = b"".join(
        publication.line
        for publication in publications.event_publications_since(cursor)
    )
    if baseline is None and not registered_append:
        return None
    return (baseline or b"") + registered_append


def _ensure_restore_parent(path: Path, roots: tuple[Path, ...]) -> None:
    for root in roots:
        if path == root:
            return
        try:
            relative_parent = path.parent.relative_to(root)
        except ValueError:
            continue
        ensure_contained_directory(
            root,
            "" if relative_parent == Path(".") else relative_parent.as_posix(),
        )
        return
    raise RuntimeError(f"Cannot safely restore artifact outside reserved roots: {path}")


def _atomic_restore(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_restore_registered(
    path: Path,
    publications: RuntimePublicationRegistry,
) -> bool:
    temporary_path: Path | None = None
    try:
        _remove_existing_directory(path)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            if not publications.copy_recovery_payload_to(path, temporary):
                return False
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        return True
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _restore_directory(
    path: Path,
    expected_snapshot: DirectorySnapshot,
) -> None:
    path_stat = _lstat_or_none(path)
    if path_stat is None or not stat.S_ISDIR(path_stat.st_mode):
        _remove_existing_path(path)
        ensure_contained_directory(path.parent, path.name)
        path_stat = path.lstat()
    ownership_changed = (
        path_stat.st_uid != expected_snapshot.user_id
        or path_stat.st_gid != expected_snapshot.group_id
    )
    if ownership_changed:
        os.chown(path, expected_snapshot.user_id, expected_snapshot.group_id)
    expected_mode = stat.S_IMODE(expected_snapshot.mode)
    if ownership_changed or stat.S_IMODE(path_stat.st_mode) != expected_mode:
        path.chmod(expected_mode)


def _remove_existing_directory(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
        shutil.rmtree(path)


def _remove_existing_path(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
        shutil.rmtree(path)
        return
    path.unlink()


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
    node_dir: Path,
) -> bool:
    del node_dir
    manifests_dir = _manifests_dir(output)
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
    node_dir: Path,
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
        path
        for path in unexpected_paths
        if is_fatal_drift_path(path, output=output, node_dir=node_dir)
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
        and _same_directory_identity_and_access(before, after)
    )


def detect_strict_file_drift(
    file_path: Path,
    before: bytes | None,
    after: bytes | None,
) -> DriftCheckResult:
    if before == after:
        return DriftCheckResult()
    return DriftCheckResult(fatal_paths=(file_path,))


def detect_event_log_drift(
    event_log_path: Path,
    before: bytes | None,
    after: bytes | None,
    expected_append: bytes,
    strict_expected_append: bool,
    registered_append: bytes | None = None,
    registered_owned_append: bytes | None = None,
) -> DriftCheckResult:
    expected_event_append = (
        registered_append if registered_append is not None else expected_append
    )
    if before is None and after is None:
        return DriftCheckResult()
    if before is None:
        if not strict_expected_append:
            return DriftCheckResult()
        if registered_owned_append is not None and not _contains_lines(
            registered_owned_append,
            expected_append,
        ):
            return DriftCheckResult(fatal_paths=(event_log_path,))
        if expected_event_append and event_log_append_matches_expected(
            after,
            expected_event_append,
        ):
            return DriftCheckResult()
        if after and event_log_append_matches_expected(after, expected_event_append):
            return DriftCheckResult()
        return DriftCheckResult(fatal_paths=(event_log_path,))
    if after is None:
        return DriftCheckResult(fatal_paths=(event_log_path,))
    if len(after) < len(before) or not after.startswith(before):
        return DriftCheckResult(fatal_paths=(event_log_path,))
    if not strict_expected_append:
        return DriftCheckResult()
    actual_append = after[len(before) :]
    if registered_owned_append is not None and not _contains_lines(
        registered_owned_append,
        expected_append,
    ):
        return DriftCheckResult(fatal_paths=(event_log_path,))
    if not event_log_append_matches_expected(actual_append, expected_event_append):
        return DriftCheckResult(fatal_paths=(event_log_path,))
    return DriftCheckResult()


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


def detect_provider_call_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    event_log_capture: EventLogAppendCapture | None,
    event_log_start_index: int,
) -> DriftCheckResult:
    check_shared_reserved_drift = should_check_shared_reserved_drift(
        monitoring_window.activity_window,
        request.telemetry,
        request.node.id,
    )
    return merge_drift_results(
        detect_node_drift(request, monitoring_window),
        detect_shared_reserved_drift(
            request,
            monitoring_window,
            check_shared_reserved_drift,
        ),
        detect_summary_drift(request, monitoring_window),
        detect_event_log_append_drift(
            request,
            monitoring_window,
            event_log_capture,
            event_log_start_index,
            check_shared_reserved_drift,
        ),
    )


@dataclass(frozen=True)
class _NodeSnapshotBoundary:
    generated_publications: dict[Path, tuple[int, str]]
    in_progress_generated_roots: set[Path]
    generated_version: int | None
    runtime_publications: dict[Path, tuple[int, str]]
    runtime_version: int | None


def detect_node_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
) -> DriftCheckResult:
    allowed_paths = request.allowed_paths
    allowance = request.generated_file_allowance
    if allowance is None and request.drift_session is not None:
        allowance = request.drift_session.generated_file_allowance
    runtime_publications = request.runtime_publications
    if runtime_publications is None and request.drift_session is not None:
        runtime_publications = request.drift_session.runtime_publications
    after_snapshot, after_directory_snapshot, boundary = _stable_node_snapshot(
        request.node_dir,
        allowance,
        runtime_publications,
        request.runtime_owned_paths,
        request.runtime_owned_roots,
    )
    in_progress_runtime_roots = boundary.in_progress_generated_roots
    expected_runtime_publications = {
        path: signature
        for path, signature in boundary.generated_publications.items()
        if not _snapshot_path_is_within_roots(path, in_progress_runtime_roots)
    }
    expected_runtime_publications.update(
        {
            path: signature
            for path, signature in boundary.runtime_publications.items()
            if path.is_relative_to(request.node_dir)
            and path not in request.protected_paths
            and not _snapshot_path_is_within_roots(path, in_progress_runtime_roots)
        }
    )
    after_snapshot = _include_current_runtime_publications(
        after_snapshot,
        expected_runtime_publications,
    )
    drift = detect_artifact_drift(
        before_snapshot=monitoring_window.node_snapshot,
        after_snapshot=after_snapshot,
        allowed_paths=allowed_paths,
        output=request.output,
        node_dir=request.node_dir,
        in_progress_runtime_roots=in_progress_runtime_roots,
        expected_runtime_publications=expected_runtime_publications,
    )
    directory_drift = detect_artifact_drift(
        before_snapshot=monitoring_window.node_directory_snapshot,
        after_snapshot=after_directory_snapshot,
        allowed_paths=_allowed_directory_paths(
            {
                *allowed_paths,
                *expected_runtime_publications,
                *in_progress_runtime_roots,
                *request.runtime_owned_paths,
                *request.runtime_owned_roots,
            },
            request.node_dir,
        ),
        output=request.output,
        node_dir=request.node_dir,
        in_progress_runtime_roots=in_progress_runtime_roots,
    )
    directory_drift = _suppress_new_ancestor_directory_drift(
        directory_drift,
        drift,
        monitoring_window.node_directory_snapshot,
    )
    directory_drift = _suppress_added_child_directory_drift(
        directory_drift,
        drift,
        monitoring_window.node_directory_snapshot,
        after_directory_snapshot,
        request.protected_paths,
    )
    drift = merge_drift_results(drift, directory_drift)
    fatal_mutations = tuple(
        path
        for path in drift.warning_paths
        if (
            path in monitoring_window.node_snapshot
            or path in monitoring_window.node_directory_snapshot
            or path in request.protected_paths
        )
        and path not in request.runtime_owned_paths
        and not _snapshot_path_is_within_roots(path, request.runtime_owned_roots)
    )
    if not fatal_mutations:
        return drift
    return DriftCheckResult(
        warning_paths=tuple(
            path for path in drift.warning_paths if path not in fatal_mutations
        ),
        fatal_paths=tuple(sorted({*drift.fatal_paths, *fatal_mutations})),
    )


def _stable_node_snapshot(
    node_dir: Path,
    allowance: GeneratedFileDriftAllowance | None,
    runtime_publications: RuntimePublicationRegistry | None,
    runtime_owned_paths: set[Path],
    runtime_owned_roots: set[Path],
) -> tuple[
    dict[Path, tuple[int, str]],
    dict[Path, DirectorySnapshot],
    _NodeSnapshotBoundary,
]:
    while True:
        before = _node_snapshot_boundary(allowance, runtime_publications)
        try:
            files = snapshot_files(
                node_dir,
                excluded_paths=runtime_owned_paths,
                excluded_roots={
                    *before.in_progress_generated_roots,
                    *runtime_owned_roots,
                },
            )
            directories = snapshot_directories(
                node_dir,
                excluded_paths=runtime_owned_paths,
                excluded_roots={
                    *before.in_progress_generated_roots,
                    *runtime_owned_roots,
                },
            )
        except RuntimeError:
            if _node_snapshot_boundary(allowance, runtime_publications) == before:
                raise
            continue
        after = _node_snapshot_boundary(allowance, runtime_publications)
        if after == before:
            return files, directories, after


def _node_snapshot_boundary(
    allowance: GeneratedFileDriftAllowance | None,
    runtime_publications: RuntimePublicationRegistry | None,
) -> _NodeSnapshotBoundary:
    if allowance is None:
        generated_publications: dict[Path, tuple[int, str]] = {}
        in_progress_generated_roots: set[Path] = set()
        generated_version = None
    else:
        (
            generated_publications,
            in_progress_generated_roots,
            generated_version,
        ) = allowance.snapshot()
    if runtime_publications is None:
        publications: dict[Path, tuple[int, str]] = {}
        runtime_version = None
    else:
        publications, runtime_version = runtime_publications.snapshot()
    return _NodeSnapshotBoundary(
        generated_publications=generated_publications,
        in_progress_generated_roots=in_progress_generated_roots,
        generated_version=generated_version,
        runtime_publications=publications,
        runtime_version=runtime_version,
    )


def _include_current_runtime_publications(
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


def _suppress_new_ancestor_directory_drift(
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


def _suppress_added_child_directory_drift(
    directory_drift: DriftCheckResult,
    file_drift: DriftCheckResult,
    before_snapshot: dict[Path, DirectorySnapshot],
    after_snapshot: dict[Path, DirectorySnapshot],
    protected_paths: set[Path],
) -> DriftCheckResult:
    changed_paths = {
        *file_drift.warning_paths,
        *file_drift.fatal_paths,
        *directory_drift.warning_paths,
        *directory_drift.fatal_paths,
    }

    def is_derivative(path: Path) -> bool:
        before = before_snapshot.get(path)
        after = after_snapshot.get(path)
        if (
            path in protected_paths
            or before is None
            or after is None
            or not _same_directory_identity_and_access(before, after)
        ):
            return False
        added_names = set(after.entry_names) - set(before.entry_names)
        if not added_names or set(before.entry_names) - set(after.entry_names):
            return False
        added_roots = {path / name for name in added_names}
        descendant_changes = {
            changed
            for changed in changed_paths
            if changed != path and changed.is_relative_to(path)
        }
        return (
            bool(descendant_changes)
            and all(
                any(
                    changed == root or changed.is_relative_to(root)
                    for root in added_roots
                )
                for changed in descendant_changes
            )
            and all(
                any(
                    changed == root or changed.is_relative_to(root)
                    for changed in descendant_changes
                )
                for root in added_roots
            )
        )

    return DriftCheckResult(
        warning_paths=tuple(
            path for path in directory_drift.warning_paths if not is_derivative(path)
        ),
        fatal_paths=tuple(
            path for path in directory_drift.fatal_paths if not is_derivative(path)
        ),
    )


def _same_directory_identity_and_access(
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


def detect_shared_reserved_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    check_shared_reserved_drift: bool,
) -> DriftCheckResult:
    del check_shared_reserved_drift
    publications = request.runtime_publications
    if publications is None and request.drift_session is not None:
        publications = request.drift_session.runtime_publications
    after_snapshot, after_directory_snapshot, expected_publications = (
        _stable_shared_reserved_snapshot(request, publications)
    )
    if publications is not None:
        expected_publications = {
            path: signature
            for path, signature in expected_publications.items()
            if is_fatal_drift_path(path, request.output, request.node_dir)
            and not path.is_relative_to(request.node_dir)
        }
        after_snapshot = _include_current_runtime_publications(
            after_snapshot,
            expected_publications,
        )
    file_drift = detect_artifact_drift(
        before_snapshot=monitoring_window.shared_reserved_snapshot or {},
        after_snapshot=after_snapshot,
        allowed_paths=request.allowed_paths,
        output=request.output,
        node_dir=request.node_dir,
        expected_runtime_publications=expected_publications,
    )
    before_directory_snapshot = monitoring_window.shared_reserved_directory_snapshot
    if before_directory_snapshot is None:
        return file_drift
    directory_drift = detect_artifact_drift(
        before_snapshot=before_directory_snapshot,
        after_snapshot=after_directory_snapshot,
        allowed_paths=_allowed_directory_paths(
            expected_publications,
            (
                request.output.results_dir,
                _manifests_dir(request.output),
                request.output.get_run_log_dir(),
            ),
        ),
        output=request.output,
        node_dir=request.node_dir,
    )
    directory_drift = _suppress_new_ancestor_directory_drift(
        directory_drift,
        file_drift,
        before_directory_snapshot,
    )
    return merge_drift_results(file_drift, directory_drift)


def _stable_shared_reserved_snapshot(
    request: DriftGuardCallRequest,
    publications: RuntimePublicationRegistry | None,
) -> tuple[
    dict[Path, tuple[int, str]],
    dict[Path, DirectorySnapshot],
    dict[Path, tuple[int, str]],
]:
    if publications is None:
        return (
            shared_reserved_snapshot(request.node_dir, request.output),
            _shared_reserved_directory_snapshot(request.node_dir, request.output),
            {},
        )
    while True:
        expected_publications, before_version = publications.snapshot()
        try:
            files = shared_reserved_snapshot(request.node_dir, request.output)
            directories = _shared_reserved_directory_snapshot(
                request.node_dir,
                request.output,
            )
        except RuntimeError:
            _, after_version = publications.snapshot()
            if after_version == before_version:
                raise
            continue
        expected_publications, after_version = publications.snapshot()
        if after_version == before_version:
            return files, directories, expected_publications


def _manifests_dir(output: ArtifactStorePort) -> Path:
    return output.stages_dir / "manifests"


def _allowed_directory_paths(
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


def detect_summary_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
) -> DriftCheckResult:
    summary_path = request.output.get_run_summary_path()
    try:
        summary_after = read_file_bytes(summary_path)
    except (OSError, RuntimeError, ValueError):
        return DriftCheckResult(fatal_paths=(summary_path,))
    return detect_strict_file_drift(
        summary_path,
        monitoring_window.summary_before,
        summary_after,
    )


def detect_event_log_append_drift(
    request: DriftGuardCallRequest,
    monitoring_window: DriftMonitoringWindow,
    event_log_capture: EventLogAppendCapture | None,
    event_log_start_index: int,
    check_shared_reserved_drift: bool,
) -> DriftCheckResult:
    event_log_path = request.output.get_run_event_log_path()
    expected_append = (
        event_log_capture.expected_append_bytes_since(event_log_start_index)
        if event_log_capture is not None
        else b""
    )
    publications = request.runtime_publications
    if publications is None and request.drift_session is not None:
        publications = request.drift_session.runtime_publications
    registered_append: bytes | None = None
    registered_owned_append: bytes | None = None
    if (
        publications is not None
        and monitoring_window.event_publication_cursor is not None
    ):
        with publications.event_observation():
            try:
                event_log_after = read_file_bytes(event_log_path)
            except (OSError, RuntimeError, ValueError):
                return DriftCheckResult(fatal_paths=(event_log_path,))
            registered_publications = publications.event_publications_since(
                monitoring_window.event_publication_cursor
            )
            registered_append = b"".join(
                publication.line for publication in registered_publications
            )
            if event_log_capture is not None:
                registered_owned_append = b"".join(
                    publication.line
                    for publication in registered_publications
                    if publication.owner_id == event_log_capture.owner_id
                )
    else:
        try:
            event_log_after = read_file_bytes(event_log_path)
        except (OSError, RuntimeError, ValueError):
            return DriftCheckResult(fatal_paths=(event_log_path,))
    return detect_event_log_drift(
        event_log_path,
        monitoring_window.event_log_before,
        event_log_after,
        expected_append,
        True if registered_append is not None else check_shared_reserved_drift,
        registered_append,
        registered_owned_append,
    )
