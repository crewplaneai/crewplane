from __future__ import annotations

import subprocess
from pathlib import Path


def cache_workspace_path(tmp_path: Path, family: str, run_key: str, name: str) -> Path:
    return tmp_path / family / "repo-1" / run_key / name


def cleanup_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_cleanup_git(repo, "init")
    run_cleanup_git(repo, "config", "user.name", "Crewplane Test")
    run_cleanup_git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("ready\n", encoding="utf-8")
    run_cleanup_git(repo, "add", "README.md")
    run_cleanup_git(repo, "commit", "-m", "initial")
    return repo


def run_cleanup_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")
