from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tests.helpers.workspace_service import file_sha256, run_git_text


@dataclass(frozen=True)
class BundleCommit:
    commit: str
    tree: str
    ref: str
    path: Path
    sha256: str
    size_bytes: int


def create_result_bundle(
    tmp_path: Path,
    repo: Path,
    slug: str,
) -> tuple[str, str, str, Path, str]:
    parent = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    tree = run_git_text(repo, "rev-parse", "HEAD^{tree}")
    result_commit = run_git_text(
        repo,
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        slug,
    )
    result_ref = f"refs/crewplane/test/{slug}"
    bundle_path = tmp_path / f"{slug}.bundle"
    run_git_text(repo, "update-ref", result_ref, result_commit)
    run_git_text(repo, "bundle", "create", bundle_path.as_posix(), result_ref)
    bundle_sha256 = file_sha256(bundle_path)
    return result_commit, tree, result_ref, bundle_path, bundle_sha256


def create_pruned_result_bundle(
    tmp_path: Path,
    repo: Path,
) -> tuple[str, str, str, Path, str]:
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = create_result_bundle(
        tmp_path, repo, "result"
    )
    run_git_text(repo, "update-ref", "-d", result_ref)
    run_git_text(repo, "reflog", "expire", "--expire=now", "--all")
    run_git_text(repo, "gc", "--prune=now")
    return result_commit, tree, result_ref, bundle_path, bundle_sha256


def create_prerequisite_bundle_chain(
    repo: Path,
    first_bundle_path: Path,
    second_bundle_path: Path,
) -> tuple[BundleCommit, BundleCommit]:
    base_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    first = _commit_file_change(repo, "first chained result\n", "first")
    first_ref = "refs/crewplane/test/first-chain"
    run_git_text(repo, "update-ref", first_ref, first.commit)
    first_bundle_path.parent.mkdir(parents=True, exist_ok=True)
    run_git_text(
        repo,
        "bundle",
        "create",
        first_bundle_path.as_posix(),
        first_ref,
        f"^{base_commit}",
    )
    second = _commit_file_change(repo, "second chained result\n", "second")
    second_ref = "refs/crewplane/test/second-chain"
    run_git_text(repo, "update-ref", second_ref, second.commit)
    second_bundle_path.parent.mkdir(parents=True, exist_ok=True)
    run_git_text(
        repo,
        "bundle",
        "create",
        second_bundle_path.as_posix(),
        second_ref,
        f"^{first.commit}",
    )
    _prune_chain_commits(repo, base_commit, first_ref, second_ref)
    return (
        _bundle_commit(first, first_ref, first_bundle_path),
        _bundle_commit(second, second_ref, second_bundle_path),
    )


@dataclass(frozen=True)
class _Commit:
    commit: str
    tree: str


def _commit_file_change(repo: Path, content: str, message: str) -> _Commit:
    (repo / "README.md").write_text(content, encoding="utf-8")
    run_git_text(repo, "add", "README.md")
    run_git_text(repo, "commit", "-m", message)
    return _Commit(
        commit=run_git_text(repo, "rev-parse", "HEAD^{commit}"),
        tree=run_git_text(repo, "rev-parse", "HEAD^{tree}"),
    )


def _bundle_commit(commit: _Commit, ref: str, path: Path) -> BundleCommit:
    return BundleCommit(
        commit=commit.commit,
        tree=commit.tree,
        ref=ref,
        path=path,
        sha256=file_sha256(path),
        size_bytes=path.stat().st_size,
    )


def _prune_chain_commits(repo: Path, base_commit: str, *refs: str) -> None:
    for ref in refs:
        run_git_text(repo, "update-ref", "-d", ref)
    run_git_text(repo, "reset", "--hard", base_commit)
    run_git_text(repo, "reflog", "expire", "--expire=now", "--all")
    run_git_text(repo, "gc", "--prune=now")
