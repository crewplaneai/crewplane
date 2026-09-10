from __future__ import annotations

from pathlib import Path

from tests.helpers.isolated_git import (
    run_git_text,
)


def create_clean_source_repo(root: Path) -> None:
    run_git_text(root, "init")
    run_git_text(root, "config", "user.name", "Crewplane Test")
    run_git_text(root, "config", "user.email", "crewplane-test@example.invalid")
    (root / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(root, "add", "README.md")
    run_git_text(root, "commit", "-m", "initial")
