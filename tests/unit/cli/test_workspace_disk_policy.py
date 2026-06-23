from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.cli.run.workspace import disk_policy as workspace_disk_policy
from crewplane.cli.run.workspace import source_policy as policy
from crewplane.core.config import Settings
from tests.helpers.workspace_source_policy import git_source_context


def test_workspace_disk_fail_threshold_blocks_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def disk_usage(path: Path) -> _DiskUsage:
        del path
        return _DiskUsage(free=99)

    monkeypatch.setattr(
        workspace_disk_policy.shutil,
        "disk_usage",
        disk_usage,
    )
    settings = Settings(
        workspace={
            "enabled": True,
            "cache_root": (tmp_path / "cache").as_posix(),
            "disk": {"fail_free_bytes": 100},
        }
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_disk_policy.warn_storage_pressure(
        settings,
        git_source_context(tmp_path),
        False,
        builder,
    )

    assert any("fail_free_bytes=100" in error for error in builder.errors)


def test_workspace_disk_fail_threshold_uses_existing_cache_ancestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    probed_paths: list[Path] = []

    def disk_usage(path: Path) -> _DiskUsage:
        probed_paths.append(path)
        return _DiskUsage(free=99)

    monkeypatch.setattr(
        workspace_disk_policy.shutil,
        "disk_usage",
        disk_usage,
    )
    cache_root = tmp_path / "missing" / "nested" / "cache"
    settings = Settings(
        workspace={
            "enabled": True,
            "cache_root": cache_root.as_posix(),
            "disk": {"fail_free_bytes": 100},
        }
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_disk_policy.warn_storage_pressure(
        settings,
        git_source_context(tmp_path),
        False,
        builder,
    )

    assert probed_paths == [tmp_path]
    assert any("fail_free_bytes=100" in error for error in builder.errors)


def test_workspace_disk_fail_threshold_uses_estimated_checkout_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def disk_usage(path: Path) -> _DiskUsage:
        del path
        return _DiskUsage(free=250)

    def git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        del project_root, args
        return ("100644 blob abc   200\tREADME.md",)

    monkeypatch.setattr(workspace_disk_policy.shutil, "disk_usage", disk_usage)
    monkeypatch.setattr(workspace_disk_policy, "git_zero_records", git_zero_records)
    settings = Settings(
        workspace={
            "enabled": True,
            "cache_root": (tmp_path / "cache").as_posix(),
            "disk": {"fail_free_bytes": 100},
        }
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_disk_policy.warn_storage_pressure(
        settings,
        git_source_context(tmp_path),
        False,
        builder,
    )

    assert any("estimated checkout size is 200" in error for error in builder.errors)
    assert any("leaving 50 byte(s)" in error for error in builder.errors)


def test_workspace_disk_warn_threshold_emits_configured_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def disk_usage(path: Path) -> _DiskUsage:
        del path
        return _DiskUsage(free=150)

    monkeypatch.setattr(
        workspace_disk_policy.shutil,
        "disk_usage",
        disk_usage,
    )
    settings = Settings(
        workspace={
            "enabled": True,
            "cache_root": (tmp_path / "cache").as_posix(),
            "disk": {"warn_free_bytes": 200},
        }
    )
    builder = policy.WorkspacePolicyBuilder()

    workspace_disk_policy.warn_storage_pressure(
        settings,
        git_source_context(tmp_path),
        False,
        builder,
    )

    assert builder.errors == []
    assert any("warn_free_bytes=200" in warning for warning in builder.warnings)


def test_estimated_checkout_size_falls_back_to_working_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        del project_root, args
        raise subprocess.CalledProcessError(1, ["git"])

    monkeypatch.setattr(workspace_disk_policy, "git_zero_records", git_zero_records)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("ignored\n", encoding="utf-8")
    (tmp_path / ".crewplane" / "execution-stages").mkdir(parents=True)
    (tmp_path / ".crewplane" / "execution-stages" / "run.json").write_text(
        "ignored\n",
        encoding="utf-8",
    )
    (tmp_path / "source.txt").write_text("source\n", encoding="utf-8")

    assert workspace_disk_policy.estimated_checkout_size_bytes(
        git_source_context(tmp_path)
    ) == len("source\n")


def test_estimated_checkout_size_fallback_counts_full_repo_for_worktrees(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        del project_root, args
        raise subprocess.CalledProcessError(1, ["git"])

    monkeypatch.setattr(workspace_disk_policy, "git_zero_records", git_zero_records)
    project_root = tmp_path / "project"
    sibling_root = tmp_path / "sibling"
    project_root.mkdir()
    sibling_root.mkdir()
    (project_root / "app.py").write_text("app\n", encoding="utf-8")
    (sibling_root / "tool.py").write_text("tool\n", encoding="utf-8")
    (project_root / ".crewplane" / "execution-stages").mkdir(parents=True)
    (project_root / ".crewplane" / "execution-stages" / "run.json").write_text(
        "ignored\n",
        encoding="utf-8",
    )
    (tmp_path / ".crewplane" / "execution-results").mkdir(parents=True)
    (tmp_path / ".crewplane" / "execution-results" / "run.json").write_text(
        "ignored\n",
        encoding="utf-8",
    )
    context = replace(
        git_source_context(tmp_path),
        project_root_relative_path="project",
    )

    assert workspace_disk_policy.estimated_checkout_size_bytes(context) == len("app\n")
    assert workspace_disk_policy.estimated_checkout_size_bytes(
        context,
        estimate_full_repository=True,
    ) == len("app\n") + len("tool\n")


def test_estimated_checkout_size_counts_full_repo_for_worktrees(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
        del project_root, args
        return (
            "100644 blob abc   100\tproject/app.py",
            "100644 blob def   500\tsibling/tool.py",
        )

    monkeypatch.setattr(workspace_disk_policy, "git_zero_records", git_zero_records)
    context = replace(
        git_source_context(tmp_path),
        project_root_relative_path="project",
    )

    assert workspace_disk_policy.estimated_checkout_size_bytes(context) == 100
    assert (
        workspace_disk_policy.estimated_checkout_size_bytes(
            context,
            estimate_full_repository=True,
        )
        == 600
    )


class _DiskUsage:
    def __init__(self, free: int) -> None:
        self.free = free
