import os
import re
import subprocess
from pathlib import Path

import yaml

from tests.unit.packaging.release_surfaces_support import (
    REPOSITORY_URL,
    ROOT,
    read_text,
    repo_path,
    write_executable,
)


def workflow_step_run(job: dict[str, object], name: str) -> str:
    steps = {step.get("name"): step for step in job["steps"] if isinstance(step, dict)}
    return str(steps[name].get("run", ""))


def test_ci_package_job_smoke_tests_wheel_before_inspection_and_upload() -> None:
    workflow = yaml.load(
        read_text(".github", "workflows", "ci.yml"), Loader=yaml.BaseLoader
    )
    package_steps = workflow["jobs"]["package"]["steps"]
    step_positions = {
        step.get("name"): index for index, step in enumerate(package_steps)
    }
    smoke_index = step_positions["Build and smoke-test package"]
    inspect_index = step_positions["Inspect dist"]
    upload_index = step_positions["Upload dist artifact"]

    assert package_steps[smoke_index]["run"] == "make install-smoke-pip"
    assert smoke_index < inspect_index < upload_index
    assert all(step.get("run") != "uv build" for step in package_steps)


def test_ci_package_job_requires_lint_and_full_test_suite() -> None:
    workflow = yaml.load(
        read_text(".github", "workflows", "ci.yml"), Loader=yaml.BaseLoader
    )
    package = workflow["jobs"]["package"]

    assert set(package["needs"]) == {"lint", "test"}
    assert package["if"] == "${{ always() }}"
    prerequisite_guard = workflow_step_run(package, "Verify required prerequisite jobs")
    for fragment in (
        "test '${{ needs.lint.result }}' = 'success'",
        "test '${{ needs.test.result }}' = 'success'",
    ):
        assert fragment in prerequisite_guard


def test_ci_test_jobs_run_full_suite_with_coverage_for_supported_python_versions() -> (
    None
):
    workflow = yaml.load(
        read_text(".github", "workflows", "ci.yml"), Loader=yaml.BaseLoader
    )
    test_job = workflow["jobs"]["test"]

    assert test_job["strategy"]["matrix"]["python-version"] == ["3.13", "3.14"]
    commands = "\n".join(
        step.get("run", "") for step in test_job["steps"] if isinstance(step, dict)
    )
    assert "uv sync --locked --python ${{ matrix.python-version }} --extra dev" in (
        commands
    )
    assert workflow_step_run(test_job, "Run tests") == (
        "uv run --locked --python ${{ matrix.python-version }} --extra dev make test"
    )
    assert (
        "uv run --locked --python ${{ matrix.python-version }} --extra dev "
        "crewplane --help"
    ) in commands


def test_production_release_workflow_reuses_release_tool_without_pypi_publish() -> None:
    workflow = read_text(".github", "workflows", "release.yml")
    workflow_config = yaml.load(workflow, Loader=yaml.BaseLoader)
    release_script = read_text("scripts", "publish_github_release.sh")

    dispatch = workflow_config["on"]["workflow_dispatch"]
    assert "push" not in workflow_config["on"]
    assert dispatch["inputs"]["tag"]["required"] == "true"
    assert dispatch["inputs"]["tag"]["type"] == "string"
    assert dispatch["inputs"]["tag"]["description"] == (
        "Tag just created by make release from the current master commit"
    )
    verify_steps = workflow_config["jobs"]["verify"]["steps"]
    master_guard = verify_steps[0]
    assert master_guard["name"] == "Reject non-master dispatch"
    assert master_guard["if"] == "github.ref != 'refs/heads/master'"
    assert "exit 1" in master_guard["run"]
    release_source_guard = verify_steps[2]
    assert release_source_guard["name"] == "Resolve and verify current release source"
    assert (
        "release_commit=\"$(git rev-parse --verify 'HEAD^{commit}')\""
        in release_source_guard["run"]
    )
    assert (
        'if [ "$release_commit" != "$GITHUB_SHA" ]; then'
        in (release_source_guard["run"])
    )
    assert (
        "Release tag must point to the dispatched master commit."
        in (release_source_guard["run"])
    )
    assert workflow.count("TAG_NAME: ${{ inputs.tag }}") == 3
    assert workflow.count("fetch-depth: 0") == 4
    assert workflow.count("git fetch --quiet --no-tags origin refs/heads/master") == 1
    assert workflow.count("git merge-base --is-ancestor") == 1
    assert "ref: refs/tags/${{ inputs.tag }}" in workflow
    assert (
        "release_commit: ${{ steps.release-source.outputs.release_commit }}" in workflow
    )
    assert "ref: ${{ needs.verify.outputs.release_commit }}" in workflow
    assert 'echo "release_commit=$release_commit" >> "$GITHUB_OUTPUT"' in workflow
    assert "group: github-release-publication" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "queue: max" in workflow
    assert workflow.count("uses: actions/checkout@") == 4
    assert "github.event.inputs" not in workflow
    assert "github.ref_name" not in workflow
    assert "path: tooling" not in workflow
    assert "path: source" not in workflow
    assert "working-directory:" not in workflow
    assert "python scripts/release.py release-artifacts" in workflow
    assert workflow.count("python scripts/release.py github-release-plan") == 1
    assert "python scripts/release.py github-release-plan" in release_script
    assert "github-release-metadata" not in workflow
    assert workflow.count("needs.verify.outputs.release_commit") == 3
    assert (
        "homebrew_eligible: ${{ steps.release-plan.outputs.homebrew_eligible }}"
        in workflow
    )
    assert "name: release-bundle" in workflow
    assert "dist/*" in workflow
    assert ".release/npm/*.tgz" in workflow
    assert ".release/release-manifest.json" in workflow
    assert "include-hidden-files: true" in workflow
    assert "overwrite: true" in workflow
    assert "scripts/publish_github_release.sh dist" in workflow
    assert "python scripts/release.py homebrew-formula" in workflow
    assert "python scripts/release.py publish-homebrew-pr" in workflow
    homebrew_upload = next(
        step
        for step in workflow_config["jobs"]["verify"]["steps"]
        if step["name"] == "Upload verified Homebrew formula"
    )
    assert homebrew_upload["with"]["include-hidden-files"] == "true"
    assert "release_flags=(--prerelease --latest=false)" in release_script
    assert "release_flags=(--prerelease=false --latest)" in release_script
    assert "release_flags=(--prerelease=false --latest=false)" in release_script
    assert '"${release_flags[@]}"' in release_script
    assert '--expected-tag "$TAG_NAME"' in workflow
    assert "recover-release-artifacts" not in workflow
    assert "verify-backfill" not in workflow
    assert "IS_BACKFILL" not in workflow
    assert 'gh release create "$tag_name" "${release_artifacts[@]}"' in release_script
    assert "release(tagName: $tag)" in release_script
    assert "nodes { name size digest }" in release_script
    assert "totalCount" in release_script
    assert 'gh release upload "$tag_name" "${release_artifacts[@]}"' in (release_script)
    assert "--clobber" in release_script
    assert 'release_artifacts=("$dist_dir"/*)' in release_script
    assert 'comm -13 "$expected_names_file" "$release_names_file"' in (release_script)
    assert 'cmp -s "$expected_assets_file" "$release_assets_file"' in release_script
    assert "Refusing to publish a draft with unexpected assets" in release_script
    assert "assets do not match the verified dist artifacts" in release_script
    assert "prerelease state does not match" in release_script
    assert "Latest state does not match" in release_script
    assert "Verified existing published GitHub Release" in release_script
    assert "query was truncated or internally inconsistent" in release_script
    assert "refusing to mutate it" in release_script
    assert 'gh release edit "$tag_name"' in release_script
    assert '--tag "$tag_name"' in release_script
    assert "--draft=false" in release_script
    assert "--verify-tag" in release_script
    assert "GH_REPO: ${{ github.repository }}" in workflow
    assert '--repo "$repository"' in release_script
    assert workflow_config["jobs"]["github-release"]["permissions"] == {
        "contents": "write"
    }
    homebrew_job = workflow_config["jobs"]["homebrew-pr"]
    assert homebrew_job["needs"] == ["verify", "github-release"]
    assert homebrew_job["if"] == ("needs.verify.outputs.homebrew_eligible == 'true'")
    assert homebrew_job["permissions"] == {"contents": "read"}
    homebrew_steps = {step["name"]: step for step in homebrew_job["steps"]}
    token_step = homebrew_steps["Create Homebrew tap token"]
    assert token_step["uses"].startswith("actions/create-github-app-token@")
    assert token_step["with"] == {
        "client-id": "${{ vars.HOMEBREW_UPDATER_CLIENT_ID }}",
        "private-key": "${{ secrets.HOMEBREW_UPDATER_PRIVATE_KEY }}",
        "owner": "crewplaneai",
        "repositories": "homebrew-crewplane",
        "permission-contents": "write",
        "permission-pull-requests": "write",
    }
    tap_checkout = homebrew_steps["Check out Homebrew tap"]
    assert tap_checkout["with"]["repository"] == "crewplaneai/homebrew-crewplane"
    assert tap_checkout["with"]["token"] == "${{ steps.tap-token.outputs.token }}"
    assert tap_checkout["with"]["persist-credentials"] == "false"
    assert (
        "gh auth setup-git"
        in homebrew_steps["Configure Homebrew tap authentication"]["run"]
    )
    assert "--execute" in homebrew_steps["Publish Homebrew pull request"]["run"]
    assert "uv build" not in workflow
    assert "urllib.request" not in workflow
    assert "pypa/gh-action-pypi-publish" not in workflow
    assert "id-token: write" not in workflow


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


def test_repository_automation_matches_supported_platform_and_publish_policy() -> None:
    nightly_text = read_text(".github", "workflows", "nightly.yml")
    nightly = yaml.safe_load(nightly_text)
    testpypi = read_text(".github", "workflows", "testpypi.yml")

    assert nightly["jobs"]["cross-platform"]["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "macos-latest",
    ]
    assert nightly["jobs"]["cross-platform"]["strategy"]["matrix"][
        "python-version"
    ] == ["3.13", "3.14"]
    cross_platform_commands = "\n".join(
        step.get("run", "")
        for step in nightly["jobs"]["cross-platform"]["steps"]
        if isinstance(step, dict)
    )
    assert "uv sync --locked --python ${{ matrix.python-version }} --extra dev" in (
        cross_platform_commands
    )
    assert (
        "uv run --locked --python ${{ matrix.python-version }} --extra dev "
        "python -m pytest -q"
    ) in cross_platform_commands
    assert '-m "not scale"' not in cross_platform_commands
    shuffled = nightly["jobs"]["shuffled-suite"]
    assert shuffled["runs-on"] == "ubuntu-latest"
    assert shuffled["env"]["CREWPLANE_RANDOM_SEED"] == "${{ github.run_id }}"
    shuffled_diagnostics = workflow_step_run(
        shuffled, "Print reliability canary diagnostics"
    )
    for fragment in (
        "OS:",
        "Architecture:",
        "python --version",
        "python -m pytest --version",
        "uv --version",
        "git --version",
        "locale",
        "Timezone:",
        "pytest-randomly seed:",
        "Selection: full test suite",
        "Iteration count: 1",
        "Skip summary: reported by pytest -ra",
    ):
        assert fragment in shuffled_diagnostics

    shuffled_commands = "\n".join(
        step.get("run", "") for step in shuffled["steps"] if isinstance(step, dict)
    )
    assert "uv sync --locked --python 3.13 --extra dev --extra stress" in (
        shuffled_commands
    )
    assert "python -m pytest -p randomly" in shuffled_commands
    assert '--randomly-seed="$seed"' in shuffled_commands
    assert '-m "not scale"' not in shuffled_commands
    assert "Reproduce locally:" in shuffled_commands

    focused = nightly["jobs"]["focused-race-loop"]
    assert focused["runs-on"] == "ubuntu-latest"
    assert focused["env"]["FOCUSED_RACE_ITERATIONS"] == "5"
    assert focused["env"]["FOCUSED_RACE_SELECTION"].endswith(
        "::ExecutorSequentialStageBasicsTests"
        "::test_multi_provider_reviewers_run_in_parallel_within_local_round"
    )
    focused_commands = "\n".join(
        step.get("run", "") for step in focused["steps"] if isinstance(step, dict)
    )
    focused_diagnostics = workflow_step_run(focused, "Print focused race diagnostics")
    for fragment in (
        "OS:",
        "Architecture:",
        "python --version",
        "python -m pytest --version",
        "uv --version",
        "git --version",
        "locale",
        "Timezone:",
        "Selection:",
        "Iteration count:",
        "Skip summary: reported by pytest -ra for each iteration",
        "Reproduce locally:",
        "FOCUSED_RACE_SELECTION=%q FOCUSED_RACE_ITERATIONS=%q",
    ):
        assert fragment in focused_diagnostics

    assert 'while [ "$iteration" -le "$FOCUSED_RACE_ITERATIONS" ]' in (focused_commands)
    assert 'python -m pytest -q -ra "$FOCUSED_RACE_SELECTION"' in focused_commands
    assert "windows-latest" not in nightly_text
    assert "skip-existing" not in testpypi


def test_weekly_uv_update_job_completes_the_dependabot_uv_pull_request() -> None:
    nightly = yaml.safe_load(read_text(".github", "workflows", "nightly.yml"))
    workflow_text = read_text(".github", "workflows", "ci-tooling-update.yml")
    workflow = yaml.safe_load(workflow_text)
    update_job = workflow["jobs"]["uv-bootstrap-update"]

    assert "ci-tooling-update" not in nightly["jobs"]
    assert 'cron: "30 20 * * 1"' in workflow_text
    assert "workflow_dispatch:" in workflow_text
    assert 'cron: "11 11 * * *"' not in workflow_text

    assert update_job["permissions"] == {
        "actions": "write",
        "contents": "write",
        "pull-requests": "read",
    }
    assert "crewplaneai/crewplane" in update_job["if"]
    commands = "\n".join(
        step.get("run", "") for step in update_job["steps"] if isinstance(step, dict)
    )
    for fragment in (
        'any(.files[]; .path == "packaging/uv-bootstrap-version.txt")',
        "isCrossRepository == false",
        'scripts/update_uv_bootstrap.py update "$version"',
        "BASH_REMATCH[1]",
        "Expected at most one Dependabot PR",
        "packaging/uv-bootstrap-version.txt",
        "--app dependabot",
        "active=false",
        "active=true",
        "gh workflow run ci.yml",
    ):
        assert fragment in commands
    for fragment in (
        "scripts/update_uv_bootstrap.py update latest",
        "automation/ci-tooling",
        "automation_pr",
        "gh pr create",
        "gh pr edit",
    ):
        assert fragment not in commands
    assert "automation/uv-bootstrap" in workflow_text
    assert "GitHub token allowed to" in workflow_text
    assert "create pull requests" in workflow_text

    steps = {step["name"]: step for step in update_job["steps"] if "name" in step}
    preserve_commands = workflow_step_run(update_job, "Preserve the trusted updater")
    assert "cp scripts/update_uv_bootstrap.py" in preserve_commands
    assert '"$RUNNER_TEMP/scripts/update_uv_bootstrap.py"' in preserve_commands
    assert steps["Update and validate pinned uv metadata"]["if"] == (
        "steps.lane.outputs.active == 'true'"
    )
    assert steps["Publish the Dependabot branch"]["if"] == (
        "steps.lane.outputs.active == 'true'"
    )
    update_commands = workflow_step_run(
        update_job,
        "Update and validate pinned uv metadata",
    )
    assert 'cd "$RUNNER_TEMP"' in update_commands
    assert ".github/workflows" not in update_commands


def test_weekly_uv_update_job_propagates_pull_request_query_failures(
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load(
        read_text(".github", "workflows", "ci-tooling-update.yml")
    )
    update_job = workflow["jobs"]["uv-bootstrap-update"]
    lane_commands = workflow_step_run(update_job, "Select the Dependabot uv PR")
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
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

    failed_selection = subprocess.run(
        ["bash", "-c", lane_commands],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        check=False,
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


def test_private_reporting_surfaces_use_github_security_advisories() -> None:
    issue_config = yaml.safe_load(read_text(".github", "ISSUE_TEMPLATE", "config.yml"))
    advisory_url = f"{REPOSITORY_URL}/security/advisories/new"
    security_links = [
        link for link in issue_config["contact_links"] if link["url"] == advisory_url
    ]

    assert len(security_links) == 1
    assert "privately" in security_links[0]["about"].lower()


def test_testpypi_workflow_uses_trusted_publishing() -> None:
    testpypi = yaml.safe_load(read_text(".github", "workflows", "testpypi.yml"))
    publisher = testpypi["jobs"]["publish-testpypi"]
    assert publisher["environment"]["name"] == "testpypi"
    assert publisher["permissions"] == {"id-token": "write", "contents": "read"}
