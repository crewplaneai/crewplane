from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from crewplane.architecture.contracts import InvocationContext
from crewplane.artifacts import OutputManager
from crewplane.artifacts.generated_files.catalog import (
    snapshot_generated_file_workspace,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.provider_call.generated_files import (
    GeneratedFileChangeBaseline,
    snapshot_invocation_generated_files,
)
from crewplane.runtime.execution.provider_call.types import ProviderOutputPolicy
from crewplane.runtime.workspace import PreparedWorkspace


def test_git_project_root_generated_files_do_not_require_provider_claim(
    tmp_path: Path,
) -> None:
    repo = _clean_repo(tmp_path)
    baseline = GeneratedFileChangeBaseline.capture(
        repo,
        filesystem_fallback_enabled=False,
    )
    (repo / "src" / "app.txt").write_text("changed\n", encoding="utf-8")
    (repo / "src" / "created.txt").write_text("created\n", encoding="utf-8")

    result_text, generated_files = _finalize_with_snapshot(
        repo,
        baseline,
        "No generated files were mentioned.\n",
    )

    assert "[alpha/src/app.txt]" in result_text or "[src/app.txt]" in result_text
    assert "[alpha/src/created.txt]" in result_text
    assert {path.name for path in generated_files} == {"app.txt", "created.txt"}


def test_git_managed_workspace_generated_files_do_not_require_provider_claim(
    tmp_path: Path,
) -> None:
    repo = _clean_repo(tmp_path)
    workspace = repo / "workspace"
    shutil.copytree(repo / "src", workspace / "src")
    _run_git(repo, "add", "workspace/src/app.txt")
    _run_git(repo, "commit", "-m", "workspace")
    baseline = GeneratedFileChangeBaseline.capture(
        workspace,
        filesystem_fallback_enabled=True,
    )
    (workspace / "src" / "app.txt").write_text("workspace changed\n", encoding="utf-8")
    (workspace / "src" / "new.txt").write_text("workspace new\n", encoding="utf-8")

    result_text, generated_files = _finalize_with_snapshot(
        repo,
        baseline,
        "No generated files were mentioned.\n",
    )

    assert "[alpha/src/app.txt]" in result_text or "[src/app.txt]" in result_text
    assert "[alpha/src/new.txt]" in result_text
    assert {path.name for path in generated_files} == {"app.txt", "new.txt"}


def test_ignored_managed_workspace_uses_filesystem_change_baseline(
    tmp_path: Path,
) -> None:
    repo = _clean_repo(tmp_path)
    (repo / ".gitignore").write_text(".crewplane/\n", encoding="utf-8")
    _run_git(repo, "add", ".gitignore")
    _run_git(repo, "commit", "-m", "ignore runtime workspace")
    workspace = repo / ".crewplane" / "workspaces" / "build" / "checkout"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.txt").write_text("before\n", encoding="utf-8")
    baseline = GeneratedFileChangeBaseline.capture(
        workspace,
        filesystem_fallback_enabled=True,
    )
    (workspace / "src" / "app.txt").write_text("after\n", encoding="utf-8")
    (workspace / "src" / "new.txt").write_text("new\n", encoding="utf-8")

    result_text, generated_files = _finalize_with_snapshot(
        repo,
        baseline,
        "No generated files were mentioned.\n",
    )

    assert "[alpha/src/app.txt]" in result_text or "[src/app.txt]" in result_text
    assert "[alpha/src/new.txt]" in result_text
    assert {path.name for path in generated_files} == {"app.txt", "new.txt"}


def test_preexisting_untracked_file_is_not_reported(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    (repo / "src" / "scratch.txt").write_text("already here\n", encoding="utf-8")
    baseline = GeneratedFileChangeBaseline.capture(
        repo,
        filesystem_fallback_enabled=False,
    )

    result_text, generated_files = _finalize_with_snapshot(
        repo,
        baseline,
        "No generated files were mentioned.\n",
    )

    assert "scratch.txt" not in result_text
    assert generated_files == ()


def test_untracked_non_ignored_file_is_reported(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    baseline = GeneratedFileChangeBaseline.capture(
        repo,
        filesystem_fallback_enabled=False,
    )
    (repo / "src" / "untracked.txt").write_text("new\n", encoding="utf-8")

    result_text, generated_files = _finalize_with_snapshot(
        repo,
        baseline,
        "No generated files were mentioned.\n",
    )

    assert "[alpha/src/untracked.txt]" in result_text
    assert tuple(path.name for path in generated_files) == ("untracked.txt",)


def test_deleted_file_is_not_copied_as_generated_file(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    baseline = GeneratedFileChangeBaseline.capture(
        repo,
        filesystem_fallback_enabled=False,
    )
    (repo / "src" / "app.txt").unlink()

    result_text, generated_files = _finalize_with_snapshot(
        repo,
        baseline,
        "No generated files were mentioned.\n",
    )

    assert "app.txt" not in result_text
    assert generated_files == ()


def test_ignored_and_cache_files_are_excluded(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    (repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _run_git(repo, "add", ".gitignore")
    _run_git(repo, "commit", "-m", "ignore cache")
    baseline = GeneratedFileChangeBaseline.capture(
        repo,
        filesystem_fallback_enabled=False,
    )
    (repo / "cache").mkdir()
    (repo / "cache" / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "module.pyc").write_bytes(b"cache")

    result_text, generated_files = _finalize_with_snapshot(
        repo,
        baseline,
        "No generated files were mentioned.\n",
    )

    assert "ignored.txt" not in result_text
    assert "module.pyc" not in result_text
    assert generated_files == ()


def test_explicit_generated_files_section_orders_valid_candidates(
    tmp_path: Path,
) -> None:
    repo = _clean_repo(tmp_path)
    baseline = GeneratedFileChangeBaseline.capture(
        repo,
        filesystem_fallback_enabled=False,
    )
    (repo / "src" / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "src" / "b.txt").write_text("b\n", encoding="utf-8")

    result_text, generated_files = _finalize_with_snapshot(
        repo,
        baseline,
        "\n".join(
            [
                "## Generated Files",
                "",
                "- `src/b.txt`",
                "- `src/missing.txt`",
            ]
        ),
    )

    assert result_text.index("[alpha/src/b.txt]") < result_text.index(
        "[alpha/src/a.txt]"
    )
    assert "[alpha/src/missing.txt]" not in result_text
    assert tuple(path.name for path in generated_files) == ("b.txt", "a.txt")


def test_non_git_project_root_prose_fallback_still_links_files(tmp_path: Path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.txt").write_text("content", encoding="utf-8")
    stage_dir = output.create_stage_dir("build.node")
    (stage_dir / "alpha_round1.md").write_text(
        "Updated `src/app.txt`.\n",
        encoding="utf-8",
    )

    output.finalize_stage("build.node")

    result_text = output.get_stage_output_path("build.node").read_text(encoding="utf-8")
    assert "[alpha/src/app.txt]" in result_text or "[src/app.txt]" in result_text


def test_non_git_project_root_fallback_uses_workspace_cwd_for_snapshot(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.txt").write_text("content", encoding="utf-8")
    stage_dir = output.create_stage_dir("build.node")
    output_file = stage_dir / "alpha_round1.md"
    output_file.write_text("Updated `src/app.txt`.\n", encoding="utf-8")

    request = SimpleNamespace(output_file=output_file)
    prepared_workspace = PreparedWorkspace(
        cwd=tmp_path,
        invocation_context=InvocationContext(
            node_id="build",
            task_id="task",
            provider="provider",
            role=ProviderRole.EXECUTOR,
        ),
    )
    snapshot = snapshot_invocation_generated_files(
        request,
        prepared_workspace,
    )
    assert snapshot is not None

    output.finalize_stage(
        "build.node",
        generated_file_workspace_roots={output_file.resolve(strict=False): snapshot},
    )
    result_text = output.get_stage_output_path("build.node").read_text(encoding="utf-8")
    assert "[alpha/src/app.txt]" in result_text or "[src/app.txt]" in result_text


def test_snapshot_invocation_generated_files_skips_missing_workspace_cwd(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    stage_dir = output.create_stage_dir("build.node")
    output_file = stage_dir / "alpha_round1.md"
    output_file.write_text("Updated `src/app.txt`.\n", encoding="utf-8")
    request = SimpleNamespace(output_file=output_file)
    prepared_workspace = PreparedWorkspace(
        cwd=tmp_path / "missing",
        invocation_context=InvocationContext(
            node_id="build",
            task_id="task",
            provider="provider",
            role=ProviderRole.EXECUTOR,
        ),
    )

    assert snapshot_invocation_generated_files(request, prepared_workspace) is None


def test_snapshot_invocation_generated_files_rejects_missing_output_by_default(
    tmp_path: Path,
) -> None:
    request = SimpleNamespace(
        output_file=tmp_path / "missing.md",
        provider_output_policy=ProviderOutputPolicy.REQUIRE_OUTPUT,
    )
    prepared_workspace = PreparedWorkspace(
        cwd=tmp_path,
        invocation_context=InvocationContext(
            node_id="build",
            task_id="task",
            provider="provider",
            role=ProviderRole.EXECUTOR,
        ),
    )

    with pytest.raises(RuntimeError, match="requires an existing provider output"):
        snapshot_invocation_generated_files(request, prepared_workspace)


def test_snapshot_invocation_generated_files_allows_explicit_missing_output_policy(
    tmp_path: Path,
) -> None:
    request = SimpleNamespace(
        output_file=tmp_path / "missing.md",
        provider_output_policy=ProviderOutputPolicy.ALLOW_MISSING_OUTPUT,
    )
    prepared_workspace = PreparedWorkspace(
        cwd=tmp_path,
        invocation_context=InvocationContext(
            node_id="review.loop",
            task_id="exec_executor_0",
            provider="exec",
            role=ProviderRole.EXECUTOR,
        ),
    )

    assert snapshot_invocation_generated_files(request, prepared_workspace) is None


def _finalize_with_snapshot(
    repo: Path,
    baseline: GeneratedFileChangeBaseline,
    provider_output: str,
) -> tuple[str, tuple[Path, ...]]:
    output = OutputManager("Workflow", base_dir=repo)
    stage_dir = output.create_stage_dir("build.node")
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text(provider_output, encoding="utf-8")
    snapshot = snapshot_generated_file_workspace(
        alpha_output,
        baseline.invocation_root,
        candidate_files=baseline.candidate_files(),
    )

    result = output.finalize_stage(
        "build.node",
        generated_file_workspace_roots={alpha_output.resolve(strict=False): snapshot},
    )
    result_text = output.get_stage_output_path("build.node").read_text(encoding="utf-8")
    return result_text, result.generated_files


def _clean_repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.name", "Crewplane Test")
    _run_git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "app.txt").write_text("initial\n", encoding="utf-8")
    _run_git(repo, "add", "src/app.txt")
    _run_git(repo, "commit", "-m", "initial")
    return repo


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
    )
