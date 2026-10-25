from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

from crewplane.architecture.contracts import InvocationContext, build_result_filename
from crewplane.artifacts import OutputManager
from crewplane.artifacts.generated_files.catalog import (
    snapshot_generated_file_workspace,
)
from crewplane.artifacts.generated_files.paths import (
    GENERATED_FILE_SOURCE_METADATA_NAME,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.git_policy import workspace_git_config_args
from crewplane.runtime.execution.provider_call import generated_file_changes
from crewplane.runtime.execution.provider_call.generated_files import (
    GeneratedFileChangeBaseline,
    capture_generated_file_change_baseline,
    snapshot_invocation_generated_files,
)
from crewplane.runtime.execution.provider_call.types import ProviderOutputPolicy
from crewplane.runtime.workspace import PreparedWorkspace
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.isolated_git import GIT_COMMAND_TIMEOUT_SECONDS, require_git


@pytest.mark.parametrize("ignore_returncode", [0, 1, 2])
def test_generated_file_git_launch_and_ignore_policy(tmp_path, ignore_returncode):
    results = _git_capture_results(tmp_path, ignore_returncode)
    ignore_command = results[-1].args
    environment = {"GIT_CONFIG_NOSYSTEM": "1"}
    failures: list[subprocess.CalledProcessError] = []

    class RecordedGitError(subprocess.CalledProcessError):
        def __init__(
            self,
            returncode: int,
            cmd: list[str],
            output: bytes | None = None,
            stderr: bytes | None = None,
        ) -> None:
            super().__init__(returncode, cmd, output=output, stderr=stderr)
            failures.append(self)

    with (
        patch.object(subprocess, "run", side_effect=results) as run,
        patch.object(subprocess, "CalledProcessError", new=RecordedGitError),
        patch.object(
            generated_file_changes,
            "sanitized_workspace_git_environment",
            return_value=environment,
        ) as sanitized_environment,
    ):
        baseline = GeneratedFileChangeBaseline.capture(tmp_path, True)

    assert sanitized_environment.call_args_list == [call(read_only=True)] * 3
    assert run.call_args_list == [
        call(
            result.args,
            check=False,
            capture_output=True,
            env=environment,
            timeout=30.0,
        )
        for result in results
    ]
    assert (baseline.git is not None) is (ignore_returncode == 1)
    assert (baseline.filesystem_entries is not None) is (ignore_returncode != 1)
    if ignore_returncode == 2:
        assert len(failures) == 1
        assert failures[0].returncode == 2
        assert failures[0].cmd == ignore_command
        assert failures[0].output == b"ignore output"
        assert failures[0].stderr == b"ignore error"
    else:
        assert failures == []

    generated = tmp_path / "generated.txt"
    generated.write_text("generated", encoding="utf-8")
    status = subprocess.CompletedProcess(results[1].args, 0, b"?? generated.txt\0", b"")
    with patch.object(subprocess, "run", return_value=status):
        assert baseline.candidate_files() == (generated,)


def test_checked_generated_file_git_failure_preserves_process_details(tmp_path):
    results = _git_capture_results(tmp_path)
    with patch.object(subprocess, "run", side_effect=results):
        baseline = GeneratedFileChangeBaseline.capture(tmp_path, False)
    command = results[1].args
    result = subprocess.CompletedProcess(command, 7, b"output", b"error")

    with (
        patch.object(subprocess, "run", return_value=result),
        pytest.raises(subprocess.CalledProcessError) as caught,
    ):
        baseline.candidate_files()

    assert caught.value.returncode == 7
    assert caught.value.cmd == command
    assert caught.value.output == b"output"
    assert caught.value.stderr == b"error"


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired(["git"], 30.0), OSError("Git launch failed")],
)
def test_checked_generated_file_git_launch_failures_propagate(tmp_path, failure):
    with patch.object(subprocess, "run", side_effect=_git_capture_results(tmp_path)):
        baseline = GeneratedFileChangeBaseline.capture(tmp_path, False)

    with (
        patch.object(subprocess, "run", side_effect=failure),
        pytest.raises(type(failure)) as caught,
    ):
        baseline.candidate_files()

    assert caught.value is failure


@pytest.mark.parametrize("filesystem_fallback", [True, False])
@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired(["git"], 30.0), OSError("Git launch failed")],
)
def test_unchecked_generated_file_git_launch_failure_keeps_fallback(
    tmp_path, filesystem_fallback, failure
):
    results = _git_capture_results(tmp_path)
    with patch.object(subprocess, "run", side_effect=[*results[:2], failure]):
        baseline = GeneratedFileChangeBaseline.capture(tmp_path, filesystem_fallback)

    generated = tmp_path / "generated.txt"
    generated.write_text("after", encoding="utf-8")
    assert baseline.git is None
    assert baseline.candidate_files() == ((generated,) if filesystem_fallback else None)


@pytest.mark.parametrize("filesystem_fallback", [True, False])
@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["git"], 30.0),
        OSError("Git launch failed"),
        subprocess.CalledProcessError(2, ["git"], output=b"output", stderr=b"error"),
    ],
)
def test_generated_file_baseline_keeps_git_failure_fallback(
    tmp_path, filesystem_fallback, failure
):
    (tmp_path / "existing.txt").write_text("before", encoding="utf-8")
    with patch.object(subprocess, "run", side_effect=failure):
        baseline = GeneratedFileChangeBaseline.capture(tmp_path, filesystem_fallback)

    generated = tmp_path / "generated.txt"
    generated.write_text("after", encoding="utf-8")
    assert baseline.git is None
    assert baseline.candidate_files() == ((generated,) if filesystem_fallback else None)


def _git_capture_results(
    root: Path, ignore_returncode: int = 1
) -> list[subprocess.CompletedProcess[bytes]]:
    command = ["git", *workspace_git_config_args(), "-C", root.as_posix()]
    return [
        subprocess.CompletedProcess(
            [*command, "rev-parse", "--show-toplevel"],
            0,
            root.as_posix().encode("utf-8"),
            b"",
        ),
        subprocess.CompletedProcess(
            [
                *command,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=no",
                "--",
                ".",
            ],
            0,
            b"",
            b"",
        ),
        subprocess.CompletedProcess(
            [*command, "check-ignore", "-q", "--", "."],
            ignore_returncode,
            b"ignore output",
            b"ignore error",
        ),
    ]


def test_shared_project_root_does_not_capture_unclaimed_changes(
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
        explicit_claims_only=True,
    )

    assert "## Generated Files" not in result_text
    assert generated_files == ()


def test_shared_project_root_captures_explicit_generated_file_claim(
    tmp_path: Path,
) -> None:
    repo = _clean_repo(tmp_path)
    baseline = GeneratedFileChangeBaseline.capture(
        repo,
        filesystem_fallback_enabled=False,
    )
    generated_file = repo / "src" / "created.txt"
    generated_file.write_text("created\n", encoding="utf-8")
    unrelated_file = repo / "ambient.tmp"
    unrelated_file.write_text("ambient\n", encoding="utf-8")

    result_text, generated_files = _finalize_with_snapshot(
        repo,
        baseline,
        "## Generated Files\n\n- `src/created.txt`\n",
        explicit_claims_only=True,
    )

    assert "[alpha/src/created.txt]" in result_text
    assert {path.name for path in generated_files} == {"created.txt"}
    assert all(path.name != unrelated_file.name for path in generated_files)


def test_overlapping_shared_root_snapshots_keep_explicit_ownership_isolated(
    tmp_path: Path,
) -> None:
    repo = _clean_repo(tmp_path)
    baselines = [
        GeneratedFileChangeBaseline.capture(
            repo,
            filesystem_fallback_enabled=False,
        )
        for _ in range(2)
    ]
    claims = [repo / "alpha.txt", repo / "beta.txt"]
    for claim in claims:
        claim.write_text(f"{claim.stem} output\n", encoding="utf-8")
    output_dir = repo / ".crewplane-test-outputs"
    output_dir.mkdir()
    outputs = [output_dir / "alpha.md", output_dir / "beta.md"]
    for output, claim in zip(outputs, claims, strict=True):
        output.write_text(
            f"## Generated Files\n\n- `{claim.name}`\n",
            encoding="utf-8",
        )
    both_snapshots_ready = Event()
    ambient_written = Event()
    publication_barrier = Barrier(2, action=both_snapshots_ready.set)

    def snapshot_claim(index: int) -> Path:
        paused = False

        def pause_after_source_metadata(
            path: Path,
            signature: tuple[int, str],
        ) -> None:
            nonlocal paused
            del signature
            if not paused and path.name == GENERATED_FILE_SOURCE_METADATA_NAME:
                paused = True
                publication_barrier.wait(timeout=5)
                assert ambient_written.wait(timeout=5)

        return snapshot_generated_file_workspace(
            outputs[index],
            repo,
            candidate_files=baselines[index].candidate_files(),
            explicit_claims_only=True,
            on_file_published=pause_after_source_metadata,
        )

    ambient = repo / "ambient-large.bin"

    def write_ambient_file() -> None:
        assert both_snapshots_ready.wait(timeout=5)
        with ambient.open("wb") as stream:
            stream.truncate(50 * 1024 * 1024 + 1)
        ambient_written.set()

    ambient_thread = Thread(target=write_ambient_file)
    ambient_thread.start()
    with ThreadPoolExecutor(max_workers=2) as executor:
        alpha_future = executor.submit(snapshot_claim, 0)
        beta_future = executor.submit(snapshot_claim, 1)
        snapshots = (alpha_future.result(), beta_future.result())
    ambient_thread.join(timeout=5)
    assert not ambient_thread.is_alive()

    assert outputs[0].read_text(encoding="utf-8").endswith("`alpha.txt`\n")
    assert outputs[1].read_text(encoding="utf-8").endswith("`beta.txt`\n")
    assert (snapshots[0] / "alpha.txt").read_text(encoding="utf-8") == "alpha output\n"
    assert (snapshots[1] / "beta.txt").read_text(encoding="utf-8") == "beta output\n"
    assert not (snapshots[0] / "beta.txt").exists()
    assert not (snapshots[1] / "alpha.txt").exists()
    assert all(not (snapshot / ambient.name).exists() for snapshot in snapshots)


def test_shared_project_root_skips_unchanged_explicit_generated_file_claim(
    tmp_path: Path,
) -> None:
    repo = _clean_repo(tmp_path)
    baseline = GeneratedFileChangeBaseline.capture(
        repo,
        filesystem_fallback_enabled=False,
    )

    result_text, generated_files = _finalize_with_snapshot(
        repo,
        baseline,
        "## Generated Files\n\n- `src/app.txt`\n",
        explicit_claims_only=True,
    )

    assert "[alpha/src/app.txt]" not in result_text
    assert generated_files == ()


def test_shared_project_root_uses_git_change_baseline(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    prepared_workspace = PreparedWorkspace(
        cwd=repo,
        invocation_context=InvocationContext(
            node_id="build",
            task_id="task",
            provider="provider",
            role=ProviderRole.EXECUTOR,
        ),
    )

    baseline = capture_generated_file_change_baseline(prepared_workspace)

    assert baseline is not None
    assert baseline.candidate_files() == ()
    changed_file = repo / "src" / "app.txt"
    changed_file.write_text("changed\n", encoding="utf-8")
    assert baseline.candidate_files() == (changed_file,)


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


@pytest.mark.parametrize(
    "reserved",
    [
        ".crewplane",
        "__pycache__",
        "node_modules",
        ".crewplane-generated-file-source.json",
        ".crewplane-generated-file-snapshot.json",
    ],
)
def test_filesystem_discovery_and_final_links_exclude_only_reserved_roots(
    tmp_path, reserved
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    baseline = GeneratedFileChangeBaseline.capture(
        workspace, filesystem_fallback_enabled=True
    )
    relative = (
        Path(reserved) if reserved.endswith(".json") else Path(reserved) / "file.txt"
    )
    excluded = workspace / relative
    nested = workspace / "nested" / relative
    for path in (excluded, nested):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated", encoding="utf-8", newline="\n")

    candidates = baseline.candidate_files()

    assert candidates == (nested,)
    result_text, files = _finalize_with_snapshot(
        workspace,
        baseline,
        f"## Generated Files\n- `{relative.as_posix()}`\n- `nested/{relative.as_posix()}`\n",
    )
    assert len(files) == 1
    assert files[0].read_text() == "generated"
    assert f"[alpha/nested/{relative.as_posix()}]" in result_text


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
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    (stage_dir / "alpha_round1.md").write_text(
        "Updated `src/app.txt`.\n",
        encoding="utf-8",
    )

    output.finalize_node(node_artifact_request("build.node"))

    result_text = (output.results_dir / build_result_filename("build.node")).read_text(
        encoding="utf-8"
    )
    assert "[alpha/src/app.txt]" in result_text or "[src/app.txt]" in result_text


def test_non_git_project_root_explicit_claim_fails_closed(tmp_path: Path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.txt").write_text("content", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    output_file = stage_dir / "alpha_round1.md"
    output_file.write_text(
        "## Generated Files\n\n- `src/app.txt`\n",
        encoding="utf-8",
    )

    request = SimpleNamespace(
        output_file=output_file,
        invocation_output_file=None,
        defer_output_publication=False,
        on_generated_file_snapshot_started=None,
        on_generated_file_snapshot_finished=None,
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
    baseline = capture_generated_file_change_baseline(prepared_workspace)
    assert baseline is not None
    assert baseline.candidate_files() is None

    snapshot = snapshot_invocation_generated_files(
        request,
        prepared_workspace,
        baseline,
    )
    assert snapshot is not None

    result = output.finalize_node(
        node_artifact_request("build.node"),
        generated_file_workspace_roots={output_file.resolve(strict=False): snapshot},
    )
    result_text = (output.results_dir / build_result_filename("build.node")).read_text(
        encoding="utf-8"
    )
    assert "[alpha/src/app.txt]" not in result_text
    assert "[src/app.txt]" not in result_text
    assert result.generated_files == ()


def test_snapshot_invocation_generated_files_skips_missing_workspace_cwd(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    output_file = stage_dir / "alpha_round1.md"
    output_file.write_text("Updated `src/app.txt`.\n", encoding="utf-8")
    request = SimpleNamespace(
        output_file=output_file,
        invocation_output_file=None,
        defer_output_publication=False,
        on_generated_file_snapshot_started=None,
        on_generated_file_snapshot_finished=None,
    )
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
        invocation_output_file=None,
        defer_output_publication=False,
        provider_output_policy=ProviderOutputPolicy.REQUIRE_OUTPUT,
        on_generated_file_snapshot_started=None,
        on_generated_file_snapshot_finished=None,
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
        invocation_output_file=None,
        defer_output_publication=False,
        provider_output_policy=ProviderOutputPolicy.ALLOW_MISSING_OUTPUT,
        on_generated_file_snapshot_started=None,
        on_generated_file_snapshot_finished=None,
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
    explicit_claims_only: bool = False,
) -> tuple[str, tuple[Path, ...]]:
    output = OutputManager("Workflow", base_dir=repo)
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text(provider_output, encoding="utf-8")
    snapshot = snapshot_generated_file_workspace(
        alpha_output,
        baseline.invocation_root,
        candidate_files=baseline.candidate_files(),
        explicit_claims_only=explicit_claims_only,
    )

    result = output.finalize_node(
        node_artifact_request("build.node"),
        generated_file_workspace_roots={alpha_output.resolve(strict=False): snapshot},
    )
    result_text = (output.results_dir / build_result_filename("build.node")).read_text(
        encoding="utf-8"
    )
    return result_text, result.generated_files


def _clean_repo(tmp_path: Path) -> Path:
    require_git(
        dict(os.environ),
        required=os.environ.get("CREWPLANE_REQUIRED_GIT") == "1",
        expected_floor_bin=os.environ.get("GIT_FLOOR_BIN"),
    )
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
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
