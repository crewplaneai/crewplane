import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.helpers.isolated_git import GIT_COMMAND_TIMEOUT_SECONDS
from tests.helpers.processes import run_process
from tests.unit.packaging.ci_workflow_support import load_workflow, workflow_step_run
from tests.unit.packaging.release_surfaces_support import (
    ROOT,
    read_text,
    repo_path,
    write_executable,
)


@pytest.mark.parametrize(
    "windows_status", ["success", "failure", "cancelled", "skipped"]
)
@pytest.mark.parametrize("lint_status", ["success", "failure", "cancelled", "skipped"])
@pytest.mark.parametrize("test_status", ["success", "failure", "cancelled", "skipped"])
def test_package_guard_requires_successful_checks(
    lint_status: str, test_status: str, windows_status: str
) -> None:
    workflow = yaml.load(
        read_text(".github", "workflows", "ci.yml"), Loader=yaml.BaseLoader
    )
    package = workflow["jobs"]["package"]
    assert {"lint", "test", "windows"} <= set(package["needs"])
    assert package["if"] == "${{ always() }}"
    guard = package["steps"][0]["run"]
    guard = (
        guard.replace("${{ needs.lint.result }}", lint_status)
        .replace("${{ needs.test.result }}", test_status)
        .replace("${{ needs.windows.result }}", windows_status)
    )
    result = run_process(["bash", "-e", "-c", guard], check=False)
    assert (result.returncode == 0) is (
        lint_status == test_status == windows_status == "success"
    )


def test_ci_package_smoke_and_typechecks_precede_artifact_upload() -> None:
    steps = load_workflow("ci.yml")["jobs"]["package"]["steps"]
    smoke = next(
        i for i, step in enumerate(steps) if step.get("run") == "make install-smoke-pip"
    )
    typing = next(
        i for i, step in enumerate(steps) if step.get("run") == "make wheel-typecheck"
    )
    inspection = next(
        i
        for i, step in enumerate(steps)
        if "python -m zipfile --test dist/*.whl" in step.get("run", "")
    )
    upload = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert smoke < typing < inspection < upload
    assert steps[upload]["with"]["path"] == "dist/*"
    assert steps[upload]["with"]["if-no-files-found"] == "error"
    assert all(step.get("run") != "uv build" for step in steps)


def test_ci_runs_full_suite_with_coverage_for_supported_python_versions() -> None:
    job = load_workflow("ci.yml")["jobs"]["test"]
    assert {"3.13", "3.14"} <= set(job["strategy"]["matrix"]["python-version"])
    commands = [step.get("run", "") for step in job["steps"]]
    invocation = "uv run --locked --python ${{ matrix.python-version }} --extra dev"
    assert (
        "uv sync --locked --python ${{ matrix.python-version }} --extra dev" in commands
    )
    assert f"{invocation} make test" in commands
    assert f"{invocation} crewplane --help" in commands


def test_nightly_covers_supported_platforms_and_python_versions() -> None:
    nightly = load_workflow("nightly.yml")
    job = nightly["jobs"]["cross-platform"]
    matrix = job["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "macos-latest", "windows-latest"}
    assert {"3.13", "3.14"} <= set(matrix["python-version"])
    commands = [step.get("run", "") for step in job["steps"]]
    assert (
        "uv sync --locked --python ${{ matrix.python-version }} --extra dev" in commands
    )
    assert (
        "uv run --locked --python ${{ matrix.python-version }} --extra dev python -m pytest -q"
        in commands
    )
    shuffled = nightly["jobs"]["shuffled-suite"]
    assert shuffled["env"]["CREWPLANE_RANDOM_SEED"] == "${{ github.run_id }}"
    assert "uv sync --locked --python 3.13 --extra dev --extra stress" in [
        step.get("run", "") for step in shuffled["steps"]
    ]
    focused = nightly["jobs"]["focused-race-loop"]
    assert focused["env"]["FOCUSED_RACE_ITERATIONS"] == "5"
    assert focused["env"]["FOCUSED_RACE_SELECTION"] == (
        "tests/integration/runtime/execution/workflow/test_sequential_parallel_reviewers.py"
        "::ExecutorSequentialStageBasicsTests"
        "::test_multi_provider_reviewers_run_in_parallel_within_local_round"
    )
    assert shuffled["runs-on"] == focused["runs-on"] == "ubuntu-latest"


@pytest.mark.parametrize("job_id", ["shuffled-suite", "focused-race-loop"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_nightly_canaries_run_configured_tests_and_propagate_failures(
    tmp_path: Path, job_id: str, exit_code: int
) -> None:
    job = load_workflow("nightly.yml")["jobs"][job_id]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "invocations"
    write_executable(
        fake_bin / "uv",
        '#!/bin/sh\nprintf "%s\\0" "$@" >> "$FAKE_UV_LOG"\n'
        'printf "\\n" >> "$FAKE_UV_LOG"\nexit "$FAKE_UV_STATUS"\n',
    )
    result = run_process(
        ["bash", "-e", "-c", job["steps"][-1]["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            **job["env"],
            "CREWPLANE_RANDOM_SEED": "123",
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_UV_LOG": str(log),
            "FAKE_UV_STATUS": str(exit_code),
        },
    )
    assert result.returncode == exit_code, result.stderr
    invocations = [
        line.rstrip("\0").split("\0") for line in log.read_text().splitlines()
    ]
    if job_id == "shuffled-suite":
        expected = [
            "python",
            "-m",
            "pytest",
            "-p",
            "randomly",
            "--randomly-seed=123",
            "-q",
            "-ra",
        ]
        assert len(invocations) == 1
    else:
        expected = [
            "python",
            "-m",
            "pytest",
            "-q",
            "-ra",
            job["env"]["FOCUSED_RACE_SELECTION"],
        ]
        assert len(invocations) == (5 if exit_code == 0 else 1)
    for invocation in invocations:
        assert invocation[invocation.index("python") :] == expected


def test_weekly_uv_update_preserves_trusted_code_and_limits_write_scope() -> None:
    workflow = load_workflow("ci-tooling-update.yml")
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["on"]["schedule"] == [{"cron": "30 20 * * 1"}]
    assert "ci-tooling-update" not in load_workflow("nightly.yml")["jobs"]
    job = workflow["jobs"]["uv-bootstrap-update"]
    assert job["permissions"] == {
        "actions": "write",
        "contents": "write",
        "pull-requests": "read",
    }
    steps = job["steps"]
    preserve = next(
        i
        for i, step in enumerate(steps)
        if "cp scripts/update_uv_bootstrap.py" in step.get("run", "")
    )
    select = next(i for i, step in enumerate(steps) if step.get("id") == "lane")
    assert preserve < select
    assert '"$RUNNER_TEMP/scripts/update_uv_bootstrap.py"' in steps[preserve]["run"]
    selection = workflow_step_run(job, "lane")
    assert "isCrossRepository == false" in selection
    assert "--app dependabot" in selection
    assert 'any(.files[]; .path == "packaging/uv-bootstrap-version.txt")' in selection
    update = next(
        step
        for step in steps
        if 'scripts/update_uv_bootstrap.py update "$version"' in step.get("run", "")
    )
    publish = next(step for step in steps if step.get("id") == "publish")
    assert update["if"] == publish["if"] == "steps.lane.outputs.active == 'true'"
    assert 'cd "$RUNNER_TEMP"' in update["run"]
    assert "BASH_REMATCH[1]" in update["run"]
    assert ".github/workflows" not in update["run"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "gh workflow run ci.yml" in commands
    for forbidden in (
        "gh pr create",
        "gh pr edit",
        "update latest",
        "automation/ci-tooling",
        "automation_pr",
    ):
        assert forbidden not in commands


def test_github_workflow_actions_are_pinned_to_commits() -> None:
    workflows = sorted(repo_path(".github", "workflows").glob("*.yml"))
    for workflow in workflows:
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "uses:" not in line:
                continue
            action = line.split("uses:", 1)[1].split("#", 1)[0].strip()
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action), (
                f"{workflow.relative_to(ROOT)}:{line_number} is not commit-pinned"
            )


def test_weekly_uv_update_job_propagates_pull_request_query_failures(
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load(
        read_text(".github", "workflows", "ci-tooling-update.yml")
    )
    update_job = workflow["jobs"]["uv-bootstrap-update"]
    lane_commands = workflow_step_run(update_job, "lane")
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=repository,
        check=True,
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    write_executable(fake_bin / "gh", "#!/bin/sh\nexit 42\n")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    github_output = tmp_path / "github-output"
    env = os.environ.copy()
    env.update(
        {
            "GITHUB_OUTPUT": str(github_output),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
        }
    )

    failed_selection = run_process(
        ["bash", "-c", lane_commands], cwd=repository, env=env, check=False
    )

    assert failed_selection.returncode == 42
    assert not github_output.exists()


def test_security_scanning_write_permissions_are_job_scoped() -> None:
    for workflow_name, job_name in (
        ("scorecard.yml", "scorecard"),
        ("codeql.yml", "analyze"),
    ):
        workflow = yaml.safe_load(read_text(".github", "workflows", workflow_name))

        assert workflow["permissions"] == {"contents": "read"}
        assert workflow["jobs"][job_name]["permissions"] == {
            "contents": "read",
            "security-events": "write",
        }


def test_testpypi_workflow_uses_trusted_publishing() -> None:
    testpypi = yaml.safe_load(read_text(".github", "workflows", "testpypi.yml"))
    publisher = testpypi["jobs"]["publish-testpypi"]
    assert publisher["environment"]["name"] == "testpypi"
    assert publisher["permissions"] == {"id-token": "write", "contents": "read"}
    assert "skip-existing" not in "\n".join(
        step.get("run", "") + str(step.get("with", {})) for step in publisher["steps"]
    )


def test_windows_required_jobs_execute_suite_and_both_installed_wheels() -> None:
    job = load_workflow("ci.yml")["jobs"]["windows"]
    assert job["runs-on"] == "windows-latest"
    assert set(job["strategy"]["matrix"]["python-version"]) == {"3.13", "3.14"}
    commands = [step.get("run", "") for step in job["steps"]]
    prefix = "uv run --locked --python ${{ matrix.python-version }} --extra dev"
    assert f"{prefix} python -m pytest -q" in commands
    for installer in ("pip", "uv"):
        assert (
            f"{prefix} python tests/helpers/installed_wheel_smoke.py --installer {installer}"
            in commands
        )
    assert not job.get("continue-on-error")
    assert all("make " not in command for command in commands)
