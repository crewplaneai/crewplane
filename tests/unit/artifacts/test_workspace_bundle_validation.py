from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

import pytest

from orchestrator_cli.artifacts import (
    workspace_bundle_validation,
    workspace_git_blob_hash,
)


@dataclass(frozen=True)
class BlobDescriptor:
    repo: Path
    source_commit: str
    source_tree: str
    git_path: str
    git_blob: str
    git_file_mode: str
    byte_size: int
    canonical_sha256: str
    object_format: str


def test_workspace_blob_descriptor_matches_repo_blob(tmp_path: Path) -> None:
    descriptor = _blob_descriptor(tmp_path)

    assert _descriptor_matches(descriptor) is True


def test_workspace_blob_descriptor_matches_pathspec_magic_repo_path(
    tmp_path: Path,
) -> None:
    descriptor = _blob_descriptor(tmp_path, git_path=":(literal)magic.txt")

    assert _descriptor_matches(descriptor) is True


def test_workspace_blob_descriptor_matches_pathspec_magic_bundle_path(
    tmp_path: Path,
) -> None:
    descriptor = _blob_descriptor(tmp_path, git_path=":(literal)magic.txt")
    bundle_path = descriptor.repo / "workspace.bundle"
    _git(descriptor.repo, "bundle", "create", bundle_path.as_posix(), "HEAD")

    assert _descriptor_matches(descriptor, bundle_path=bundle_path) is True


def test_workspace_blob_descriptor_times_out_while_stdout_is_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = _blob_descriptor(tmp_path)
    original_popen = subprocess.Popen

    def stalled_cat_file_blob(command, stdout, stderr, env):
        del command
        return original_popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=stdout,
            stderr=stderr,
            env=env,
        )

    monkeypatch.setattr(
        workspace_git_blob_hash.subprocess,
        "Popen",
        stalled_cat_file_blob,
    )
    monkeypatch.setattr(
        workspace_bundle_validation,
        "GIT_BUNDLE_VALIDATION_TIMEOUT_SECONDS",
        0.05,
    )

    started = monotonic()

    assert _descriptor_matches(descriptor) is False
    assert monotonic() - started < 1.0


def test_git_stdout_sha256_raises_and_reaps_when_stdout_pipe_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingStdoutProcess:
        stdout = None
        killed = False
        waited = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self):
            self.waited = True
            return -9

    process = MissingStdoutProcess()

    def missing_stdout_cat_file_blob(command, stdout, stderr, env):
        del command, stdout, stderr, env
        return process

    monkeypatch.setattr(
        workspace_git_blob_hash.subprocess,
        "Popen",
        missing_stdout_cat_file_blob,
    )

    with pytest.raises(ValueError, match="Failed to capture Git blob stdout."):
        workspace_git_blob_hash.git_stdout_sha256(["git"], {}, "abc123", 30.0)

    assert process.killed is True
    assert process.waited is True


def test_git_stdout_sha256_reaps_process_when_stdout_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CapturedStdout:
        closed = False

        def close(self):
            self.closed = True

    class StartedProcess:
        def __init__(self) -> None:
            self.stdout = CapturedStdout()
            self.killed = False
            self.waited = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self):
            self.waited = True
            return -9

    class FailingSelector:
        closed = False

        def register(self, fileobj, events):
            del fileobj, events
            raise RuntimeError("selector setup failed")

        def close(self):
            self.closed = True

    process = StartedProcess()
    selector = FailingSelector()

    def started_cat_file_blob(command, stdout, stderr, env):
        del command, stdout, stderr, env
        return process

    monkeypatch.setattr(
        workspace_git_blob_hash.subprocess,
        "Popen",
        started_cat_file_blob,
    )
    monkeypatch.setattr(
        workspace_git_blob_hash.selectors,
        "DefaultSelector",
        lambda: selector,
    )

    with pytest.raises(RuntimeError, match="selector setup failed"):
        workspace_git_blob_hash.git_stdout_sha256(["git"], {}, "abc123", 30.0)

    assert process.killed is True
    assert process.waited is True
    assert process.stdout.closed is True
    assert selector.closed is True


def _blob_descriptor(tmp_path: Path, git_path: str = "file.txt") -> BlobDescriptor:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = tmp_path / "repo"
    payload = b"workspace-payload"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Orchestrator Test")
    _git(repo, "config", "user.email", "orchestrator-test@example.invalid")
    (repo / git_path).write_bytes(payload)
    _git(repo, "--literal-pathspecs", "add", git_path)
    _git(repo, "commit", "-m", "initial")
    mode, object_id = _tree_blob_entry(repo, git_path)
    return BlobDescriptor(
        repo=repo,
        source_commit=_git(repo, "rev-parse", "HEAD"),
        source_tree=_git(repo, "rev-parse", "HEAD^{tree}"),
        git_path=git_path,
        git_blob=object_id,
        git_file_mode=mode,
        byte_size=len(payload),
        canonical_sha256=hashlib.sha256(payload).hexdigest(),
        object_format=_git(repo, "rev-parse", "--show-object-format=storage"),
    )


def _descriptor_matches(
    descriptor: BlobDescriptor,
    bundle_path: Path | None = None,
) -> bool:
    return workspace_bundle_validation.workspace_blob_descriptor_matches(
        descriptor.repo.as_posix(),
        descriptor.source_commit,
        descriptor.source_tree,
        descriptor.git_path,
        descriptor.git_blob,
        descriptor.git_file_mode,
        descriptor.byte_size,
        descriptor.canonical_sha256,
        descriptor.object_format,
        bundle_path=bundle_path,
    )


def _tree_blob_entry(repo: Path, git_path: str) -> tuple[str, str]:
    header = _git(
        repo,
        "--literal-pathspecs",
        "ls-tree",
        "HEAD",
        "--",
        git_path,
    ).splitlines()[0]
    parts = header.split()
    return parts[0], parts[2]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
        env=dict(os.environ),
    )
    return result.stdout.decode("utf-8", errors="replace").strip()
