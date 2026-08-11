from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from crewplane.core.provider_process_state import ProviderProcessState

from ..naming import (
    build_provider_process_state_filename,
    validate_run_key_name,
)
from .manifest import (
    LockManifestError,
    LockRunMetadata,
    ensure_no_symlink_manifest_components,
    ensure_owner_path_contained,
)
from .process_identity import ProcessIdentity, ProcessInspector


@dataclass(frozen=True)
class _ProcessStateFile:
    path: Path
    device: int
    inode: int
    link_count: int


def ensure_no_live_provider_processes(
    state_dir: Path,
    metadata: LockRunMetadata,
    inspector: ProcessInspector,
) -> None:
    process_dir = _provider_process_dir(state_dir, metadata)
    if process_dir is None:
        return
    for path in _safe_process_state_paths(state_dir, process_dir):
        state = _read_process_state(path)
        _validate_process_state(path, state, metadata)
        identity = ProcessIdentity(
            pid=state.pid,
            hostname=state.hostname,
            start_identity=state.process_start_identity,
        )
        try:
            is_live = inspector.is_live(identity)
        except RuntimeError as exc:
            raise LockManifestError(
                "Cannot safely verify a provider process from the interrupted run "
                f"(PID {state.pid}, node '{state.node_id}')."
            ) from exc
        if is_live:
            recorded_status = (
                " despite its exited receipt" if state.status == "exited" else ""
            )
            raise LockManifestError(
                "A provider process from the interrupted run is still active"
                f"{recorded_status} "
                f"(PID {state.pid}, node '{state.node_id}', task "
                f"'{state.task_id}'). Wait for it to finish or terminate it, then "
                "retry."
            )
        if state.process_group_id is None:
            continue
        try:
            group_is_live = inspector.is_process_group_live(state.process_group_id)
        except RuntimeError as exc:
            raise LockManifestError(
                "Cannot safely verify a provider process group from the "
                f"interrupted run (PGID {state.process_group_id}, node "
                f"'{state.node_id}')."
            ) from exc
        if group_is_live:
            raise LockManifestError(
                "A recorded provider process group may still be active "
                f"(PGID {state.process_group_id}, node '{state.node_id}', task "
                f"'{state.task_id}'). Confirm that it belongs to the interrupted "
                "provider, then wait for it to finish or terminate it before "
                "retrying."
            )


def _provider_process_dir(
    state_dir: Path,
    metadata: LockRunMetadata,
) -> Path | None:
    if metadata.run_id is None and metadata.run_key_name is None:
        return None
    if metadata.run_id is None or metadata.run_key_name is None:
        raise LockManifestError("Lock owner run metadata is incomplete.")
    try:
        run_key_name = validate_run_key_name(metadata.run_key_name)
    except ValueError as exc:
        raise LockManifestError(
            "Lock owner run metadata is not safely contained."
        ) from exc
    return (
        state_dir
        / "execution-stages"
        / run_key_name
        / "manifests"
        / "provider-processes"
    )


def _safe_process_state_paths(
    state_dir: Path,
    process_dir: Path,
) -> tuple[Path, ...]:
    stages_root = state_dir / "execution-stages"
    ensure_no_symlink_manifest_components(stages_root, process_dir)
    ensure_owner_path_contained(stages_root, process_dir)
    try:
        directory_stat = process_dir.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return ()
    except PermissionError:
        raise
    except OSError as exc:
        raise LockManifestError(
            "Cannot inspect provider process state safely."
        ) from exc
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise LockManifestError("Provider process state path is not a safe directory.")
    try:
        paths = tuple(sorted(process_dir.iterdir(), key=lambda path: path.name))
    except PermissionError:
        raise
    except OSError as exc:
        raise LockManifestError(
            "Cannot inspect provider process state safely."
        ) from exc
    entries = tuple(_inspect_process_state_file(stages_root, path) for path in paths)
    canonical_entries = {
        entry.path.name: entry for entry in entries if entry.path.suffix == ".json"
    }
    paired_names = _validate_interrupted_publications(entries, canonical_entries)
    if any(
        entry.link_count != 1
        for name, entry in canonical_entries.items()
        if name not in paired_names
    ):
        raise LockManifestError("Provider process state is not a safe file.")
    return tuple(entry.path for entry in canonical_entries.values())


def _inspect_process_state_file(stages_root: Path, path: Path) -> _ProcessStateFile:
    ensure_no_symlink_manifest_components(stages_root, path)
    ensure_owner_path_contained(stages_root, path)
    try:
        path_stat = path.lstat()
    except PermissionError:
        raise
    except OSError as exc:
        raise LockManifestError(
            "Cannot inspect provider process state safely."
        ) from exc
    if not stat.S_ISREG(path_stat.st_mode):
        raise LockManifestError("Provider process state is not a safe file.")
    return _ProcessStateFile(
        path=path,
        device=path_stat.st_dev,
        inode=path_stat.st_ino,
        link_count=path_stat.st_nlink,
    )


def _validate_interrupted_publications(
    entries: tuple[_ProcessStateFile, ...],
    canonical_entries: dict[str, _ProcessStateFile],
) -> set[str]:
    paired_names: set[str] = set()
    for entry in entries:
        if entry.path.suffix == ".json":
            continue
        canonical_name = _atomic_target_name(entry.path)
        if canonical_name is None:
            raise LockManifestError("Provider process state is not a safe file.")
        canonical = canonical_entries.get(canonical_name)
        if canonical is None or canonical_name in paired_names:
            raise LockManifestError("Provider process state is not a safe file.")
        _validate_interrupted_publication(canonical, entry)
        paired_names.add(canonical_name)
    return paired_names


def _atomic_target_name(path: Path) -> str | None:
    if not path.name.startswith(".") or not path.name.endswith(".tmp"):
        return None
    target_and_token = path.name[1 : -len(".tmp")]
    target_name, separator, token = target_and_token.rpartition(".")
    if not separator or not token or not target_name.endswith(".json"):
        return None
    return target_name


def _validate_interrupted_publication(
    canonical: _ProcessStateFile,
    temporary: _ProcessStateFile,
) -> None:
    if _is_linked_publication_alias(canonical, temporary):
        return
    if canonical.link_count != 1 or temporary.link_count != 1:
        raise LockManifestError("Provider process state is not a safe file.")
    started = _read_process_state(canonical.path)
    exited = _read_process_state(temporary.path)
    if not _is_terminal_evolution(started, exited):
        raise LockManifestError(
            "Interrupted provider process state does not match its published receipt."
        )


def _is_linked_publication_alias(
    canonical: _ProcessStateFile,
    temporary: _ProcessStateFile,
) -> bool:
    return (
        canonical.device == temporary.device
        and canonical.inode == temporary.inode
        and canonical.link_count == temporary.link_count == 2
    )


def _is_terminal_evolution(
    started: ProviderProcessState,
    exited: ProviderProcessState,
) -> bool:
    if started.status != "started" or exited.status != "exited":
        return False
    restored_started = exited.model_copy(
        update={"status": "started", "exited_at": None, "returncode": None}
    )
    return restored_started == started


def _read_process_state(path: Path) -> ProviderProcessState:
    try:
        return ProviderProcessState.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except PermissionError:
        raise
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        raise LockManifestError(
            "Provider process state is malformed or unreadable."
        ) from exc


def _validate_process_state(
    path: Path,
    state: ProviderProcessState,
    metadata: LockRunMetadata,
) -> None:
    if state.run_id != metadata.run_id or state.run_key_name != metadata.run_key_name:
        raise LockManifestError(
            "Provider process state does not match the interrupted run."
        )
    expected_name = build_provider_process_state_filename(
        state.node_id,
        state.task_id,
        state.provider,
        state.role,
        state.audit_round_num,
        state.round_num,
        state.attempt,
    )
    if path.name != expected_name:
        raise LockManifestError(
            "Provider process state filename does not match its contents."
        )
