from __future__ import annotations

import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, Self

from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.core.workspace.git_policy import workspace_git_config_args

from .bundle_validation import (
    GIT_BUNDLE_VALIDATION_TIMEOUT_SECONDS,
    sanitized_bundle_git_environment,
)


class WorkspaceSourceDescriptor(Protocol):
    @property
    def source_kind(self) -> str: ...

    @property
    def source_node_id(self) -> str | None: ...

    @property
    def source_commit(self) -> str: ...

    @property
    def source_tree(self) -> str: ...

    @property
    def bundle_path(self) -> Path | None: ...

    @property
    def bundle_sha256(self) -> str | None: ...

    @property
    def bundle_size_bytes(self) -> int | None: ...

    @property
    def bundle_ref(self) -> str | None: ...

    @property
    def upstream_sources(self) -> Sequence[Self]: ...


@dataclass(frozen=True)
class PersistedWorkspaceSourceDescriptor:
    source_kind: str
    source_node_id: str | None
    source_commit: str
    source_tree: str
    bundle_path: Path | None
    bundle_sha256: str | None
    bundle_size_bytes: int | None
    bundle_ref: str | None
    upstream_sources: tuple[PersistedWorkspaceSourceDescriptor, ...] = ()


def verify_persisted_workspace_result_chain(
    source: WorkspaceSourceSnapshot,
    run_dir: Path,
    payload: Mapping[str, object],
) -> None:
    source_payload = _mapping(payload.get("source"))
    result = _mapping(payload.get("result"))
    bundle = _mapping(payload.get("bundle"))
    refs = _mapping(payload.get("refs"))
    descriptor = PersistedWorkspaceSourceDescriptor(
        source_kind="node",
        source_node_id=_required_string(payload.get("node_id")),
        source_commit=_required_string(result.get("result_commit")),
        source_tree=_required_string(result.get("result_tree")),
        bundle_path=_contained_bundle_path(run_dir, bundle.get("path")),
        bundle_sha256=_required_string(bundle.get("sha256")),
        bundle_size_bytes=_required_int(bundle.get("size_bytes")),
        bundle_ref=_required_string(refs.get("result")),
        upstream_sources=(_persisted_source_descriptor(run_dir, source_payload),),
    )
    verify_workspace_source_chain(source, descriptor)


def verify_workspace_source_chain(
    source: WorkspaceSourceSnapshot,
    source_ref: WorkspaceSourceDescriptor,
) -> None:
    with TemporaryDirectory(prefix="crewplane-chain-verify-") as temp_dir_name:
        git_dir = Path(temp_dir_name) / "chain.git"
        _init_bare_repository(git_dir, source.object_format)
        _seed_project_base(git_dir, source)
        visited: set[str] = set()
        _verify_source_descriptor(git_dir, source, source_ref, set(), visited)


def _verify_source_descriptor(
    git_dir: Path,
    source: WorkspaceSourceSnapshot,
    descriptor: WorkspaceSourceDescriptor,
    active: set[str],
    visited: set[str],
) -> None:
    if descriptor.source_commit in active:
        raise RuntimeError("Workspace source descriptor chain contains a cycle.")
    if descriptor.source_commit in visited:
        return
    active.add(descriptor.source_commit)
    for upstream in descriptor.upstream_sources:
        _verify_source_descriptor(git_dir, source, upstream, active, visited)
    active.remove(descriptor.source_commit)
    if descriptor.source_kind == "project":
        _verify_project_descriptor(git_dir, source, descriptor)
    else:
        _verify_bundle_descriptor(git_dir, descriptor)
    visited.add(descriptor.source_commit)


def _verify_project_descriptor(
    git_dir: Path,
    source: WorkspaceSourceSnapshot,
    descriptor: WorkspaceSourceDescriptor,
) -> None:
    if (
        descriptor.source_node_id is not None
        or bool(descriptor.upstream_sources)
        or descriptor.source_commit != source.run_base_commit
        or descriptor.source_tree != source.source_tree
        or any(
            value is not None
            for value in (
                descriptor.bundle_path,
                descriptor.bundle_sha256,
                descriptor.bundle_size_bytes,
                descriptor.bundle_ref,
            )
        )
    ):
        raise RuntimeError("Workspace project source descriptor is contradictory.")
    _verify_commit_tree(git_dir, descriptor.source_commit, descriptor.source_tree)


def _verify_bundle_descriptor(
    git_dir: Path,
    descriptor: WorkspaceSourceDescriptor,
) -> None:
    if descriptor.source_kind not in {"node", "candidate"}:
        raise RuntimeError("Workspace source bundle kind is invalid.")
    if not descriptor.source_node_id:
        raise RuntimeError("Workspace source bundle lacks a source node.")
    bundle_path = _validated_bundle_file(descriptor)
    if _bundle_has_prerequisites(bundle_path):
        raise RuntimeError("Workspace source bundle advertises prerequisites.")
    _run_git_dir(git_dir, "bundle", "verify", bundle_path.as_posix())
    listed = (
        _run_git_dir(
            git_dir,
            "bundle",
            "list-heads",
            bundle_path.as_posix(),
        )
        .stdout.decode("utf-8")
        .splitlines()
    )
    if len(listed) != 1:
        raise RuntimeError("Workspace source bundle must offer exactly one ref.")
    object_id, separator, ref_name = listed[0].partition(" ")
    if (
        separator != " "
        or object_id != descriptor.source_commit
        or ref_name != descriptor.bundle_ref
    ):
        raise RuntimeError("Workspace source bundle ref or target OID mismatch.")
    _run_git_dir(git_dir, "bundle", "unbundle", bundle_path.as_posix())
    if len(descriptor.upstream_sources) != 1:
        raise RuntimeError(
            "Workspace non-project source descriptor requires exactly one parent source."
        )
    expected_parent = descriptor.upstream_sources[0].source_commit
    _verify_commit(git_dir, descriptor, expected_parent)


def _verify_commit(
    git_dir: Path,
    descriptor: WorkspaceSourceDescriptor,
    expected_parent: str,
) -> None:
    object_type = (
        _run_git_dir(
            git_dir,
            "cat-file",
            "-t",
            descriptor.source_commit,
        )
        .stdout.decode("utf-8")
        .strip()
    )
    if object_type != "commit":
        raise RuntimeError("Workspace source bundle result is not a commit.")
    parent_record = (
        _run_git_dir(
            git_dir,
            "rev-list",
            "--parents",
            "-n",
            "1",
            descriptor.source_commit,
        )
        .stdout.decode("utf-8")
        .strip()
        .split()
    )
    if parent_record != [descriptor.source_commit, expected_parent]:
        raise RuntimeError("Workspace source bundle result has an unexpected parent.")
    _verify_commit_tree(git_dir, descriptor.source_commit, descriptor.source_tree)


def _verify_commit_tree(git_dir: Path, commit: str, expected_tree: str) -> None:
    actual_tree = (
        _run_git_dir(
            git_dir,
            "rev-parse",
            f"{commit}^{{tree}}",
        )
        .stdout.decode("utf-8")
        .strip()
    )
    if actual_tree != expected_tree:
        raise RuntimeError("Workspace source bundle result tree mismatch.")


def _validated_bundle_file(descriptor: WorkspaceSourceDescriptor) -> Path:
    path = descriptor.bundle_path
    if (
        path is None
        or descriptor.bundle_sha256 is None
        or descriptor.bundle_size_bytes is None
        or descriptor.bundle_ref is None
    ):
        raise RuntimeError("Workspace source bundle descriptor is incomplete.")
    try:
        mode = path.lstat().st_mode
        file_stat = path.stat()
    except OSError as exc:
        raise RuntimeError("Workspace source bundle is missing or unreadable.") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or file_stat.st_nlink != 1:
        raise RuntimeError("Workspace source bundle must be a regular file.")
    size_bytes, sha256 = file_size_and_sha256(path)
    if size_bytes != descriptor.bundle_size_bytes:
        raise RuntimeError("Workspace source bundle size mismatch.")
    if sha256 != descriptor.bundle_sha256:
        raise RuntimeError("Workspace source bundle digest mismatch.")
    return path


def _bundle_has_prerequisites(bundle_path: Path) -> bool:
    with bundle_path.open("rb") as handle:
        signature = handle.readline()
        if not signature.startswith(b"# v") or not signature.rstrip().endswith(
            b"git bundle"
        ):
            raise RuntimeError("Workspace source bundle header is invalid.")
        while True:
            line = handle.readline()
            if not line:
                raise RuntimeError("Workspace source bundle header is incomplete.")
            if line in {b"\n", b"\r\n"}:
                return False
            if line.startswith(b"-"):
                return True


def _init_bare_repository(git_dir: Path, object_format: str) -> None:
    if object_format not in {"sha1", "sha256"}:
        raise RuntimeError(f"Unsupported workspace object format: {object_format}.")
    template_dir = git_dir.parent / "empty-template"
    template_dir.mkdir()
    _run_git(
        "init",
        "--bare",
        f"--object-format={object_format}",
        f"--template={template_dir.as_posix()}",
        git_dir.as_posix(),
    )


def _seed_project_base(git_dir: Path, source: WorkspaceSourceSnapshot) -> None:
    seed_ref = f"refs/crewplane/verification/base-{source.run_base_commit[:24]}"
    _run_git_dir(
        git_dir,
        "fetch",
        "--no-auto-maintenance",
        "--no-tags",
        source.git_top_level,
        f"{source.run_base_commit}:{seed_ref}",
    )
    _verify_commit_tree(git_dir, source.run_base_commit, source.source_tree)


def _run_git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *workspace_git_config_args(), *args],
        check=True,
        capture_output=True,
        env=sanitized_bundle_git_environment(),
        timeout=GIT_BUNDLE_VALIDATION_TIMEOUT_SECONDS,
    )


def _run_git_dir(git_dir: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return _run_git(
        "--no-optional-locks",
        f"--git-dir={git_dir.as_posix()}",
        *args,
    )


def _persisted_source_descriptor(
    run_dir: Path,
    payload: Mapping[str, object],
) -> PersistedWorkspaceSourceDescriptor:
    upstream_payloads = payload.get("upstream_sources")
    if upstream_payloads is None:
        upstream_payloads = []
    if not isinstance(upstream_payloads, list) or not all(
        isinstance(item, dict) for item in upstream_payloads
    ):
        raise RuntimeError("Workspace source descriptor chain is invalid.")
    return PersistedWorkspaceSourceDescriptor(
        source_kind=_required_string(payload.get("kind")),
        source_node_id=_optional_string(payload.get("node_id")),
        source_commit=_required_string(payload.get("commit")),
        source_tree=_required_string(payload.get("tree")),
        bundle_path=(
            _contained_bundle_path(run_dir, payload.get("bundle_path"))
            if payload.get("bundle_path") is not None
            else None
        ),
        bundle_sha256=_optional_string(payload.get("bundle_sha256")),
        bundle_size_bytes=_optional_int(payload.get("bundle_size_bytes")),
        bundle_ref=_optional_string(payload.get("bundle_ref")),
        upstream_sources=tuple(
            _persisted_source_descriptor(run_dir, item)
            for item in upstream_payloads
            if isinstance(item, dict)
        ),
    )


def _contained_bundle_path(run_dir: Path, value: object) -> Path:
    relative_path = _required_string(value)
    path = Path(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("Workspace source bundle path is unsafe.")
    candidate = run_dir / path
    try:
        resolved_run_dir = run_dir.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Workspace source bundle path is missing.") from exc
    if not resolved_candidate.is_relative_to(resolved_run_dir):
        raise RuntimeError("Workspace source bundle path escapes the run directory.")
    return candidate


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("Workspace source descriptor lacks a required string.")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _required_string(value)


def _required_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError("Workspace source descriptor lacks a required size.")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _required_int(value)
