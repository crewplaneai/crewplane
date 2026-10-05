import os
from pathlib import Path

import pytest

from tests.helpers.isolated_git import run_git, run_git_text
from tests.helpers.processes import run_process
from tests.unit.packaging.ci_workflow_support import load_workflow, workflow_step_run


def test_release_workflow_checks_out_verified_commits() -> None:
    workflow = load_workflow("release.yml")
    assert "push" not in workflow["on"]
    tag = workflow["on"]["workflow_dispatch"]["inputs"]["tag"]
    assert tag["required"] == "true"
    assert tag["type"] == "string"
    assert workflow["concurrency"] == {
        "group": "github-release-publication",
        "cancel-in-progress": "false",
        "queue": "max",
    }
    verify = workflow["jobs"]["verify"]
    assert (
        verify["outputs"]["release_commit"]
        == "${{ steps.release-source.outputs.release_commit }}"
    )
    for job_id, ref in (
        ("verify", "refs/tags/${{ inputs.tag }}"),
        ("github-release", "${{ needs.verify.outputs.release_commit }}"),
        ("homebrew-pr", "${{ needs.verify.outputs.release_commit }}"),
    ):
        job = workflow["jobs"][job_id]
        assert job["env"]["TAG_NAME"] == "${{ inputs.tag }}"
        source_checkout = next(
            step
            for step in job["steps"]
            if step.get("uses", "").startswith("actions/checkout@")
            and "repository" not in step.get("with", {})
        )
        assert source_checkout["with"]["ref"] == ref
        assert source_checkout["with"]["fetch-depth"] == "0"
        assert source_checkout["with"]["persist-credentials"] == "false"


def test_release_dispatch_guard_fails_outside_master() -> None:
    guard = load_workflow("release.yml")["jobs"]["verify"]["steps"][0]
    assert guard["if"] == "github.ref != 'refs/heads/master'"
    result = run_process(["bash", "-e", "-c", guard["run"]])
    assert result.returncode != 0


@pytest.mark.parametrize("matching_commit", [True, False])
def test_release_source_guard_checks_and_exports_dispatched_commit(
    tmp_path: Path, matching_commit: bool
) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "commit", "--allow-empty", "-m", "release source")
    commit = run_git_text(tmp_path, "rev-parse", "HEAD").strip()
    output = tmp_path / "github-output"
    verify = load_workflow("release.yml")["jobs"]["verify"]
    result = run_process(
        ["bash", "-e", "-c", workflow_step_run(verify, "release-source")],
        cwd=tmp_path,
        env={
            **os.environ,
            "GITHUB_SHA": commit if matching_commit else "0" * 40,
            "GITHUB_OUTPUT": str(output),
        },
    )
    if matching_commit:
        assert result.returncode == 0, result.stderr
        assert output.read_text().splitlines() == [f"release_commit={commit}"]
    else:
        assert result.returncode != 0
        assert not output.exists()


def test_release_workflow_publishes_verified_artifact_bundle() -> None:
    jobs = load_workflow("release.yml")["jobs"]
    verify = jobs["verify"]
    commands = "\n".join(step.get("run", "") for step in verify["steps"])
    assert "python scripts/release.py release-artifacts" in commands
    plan = workflow_step_run(verify, "release-plan")
    assert "python scripts/release.py github-release-plan" in plan
    assert '--expected-tag "$TAG_NAME"' in plan
    bundle = next(
        step
        for step in verify["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
        and step.get("with", {}).get("name") == "release-bundle"
    )
    assert set(bundle["with"]["path"].splitlines()) == {
        "dist/*",
        ".release/npm/*.tgz",
        ".release/release-manifest.json",
    }
    assert bundle["with"]["include-hidden-files"] == "true"
    assert bundle["with"]["overwrite"] == "true"
    assert bundle["with"]["if-no-files-found"] == "error"

    publisher = jobs["github-release"]
    assert publisher["needs"] == ["verify"]
    assert publisher["permissions"] == {"contents": "write"}
    assert publisher["env"]["GH_REPO"] == "${{ github.repository }}"
    steps = publisher["steps"]
    ancestry = next(
        i
        for i, step in enumerate(steps)
        if "git merge-base --is-ancestor HEAD FETCH_HEAD" in step.get("run", "")
    )
    assert (
        "git fetch --quiet --no-tags origin refs/heads/master" in steps[ancestry]["run"]
    )
    download = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/download-artifact@")
        and step.get("with", {}).get("name") == "release-bundle"
    )
    publish = next(
        i
        for i, step in enumerate(steps)
        if step.get("run") == "scripts/publish_github_release.sh dist"
    )
    assert ancestry < download < publish
    assert steps[download]["with"]["path"] == "."
    assert steps[publish]["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_production_release_workflow_does_not_rebuild_or_publish_to_registries() -> (
    None
):
    workflow = load_workflow("release.yml")
    assert workflow["permissions"] == {"contents": "read"}
    for job in workflow["jobs"].values():
        assert "id-token" not in job.get("permissions", {})
        for step in job["steps"]:
            assert not step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
            command = step.get("run", "")
            for forbidden in (
                "uv build",
                "twine upload",
                "npm publish",
                "release.py publish-pypi",
                "release.py publish-npm",
                "recover-release-artifacts",
                "verify-backfill",
            ):
                assert forbidden not in command


def test_homebrew_formula_is_published_only_for_eligible_releases() -> None:
    jobs = load_workflow("release.yml")["jobs"]
    verify = jobs["verify"]
    assert (
        verify["outputs"]["homebrew_eligible"]
        == "${{ steps.release-plan.outputs.homebrew_eligible }}"
    )
    formula = next(
        step
        for step in verify["steps"]
        if "python scripts/release.py homebrew-formula" in step.get("run", "")
    )
    assert formula["if"] == "steps.release-plan.outputs.homebrew_eligible == 'true'"
    assert '--expected-tag "$TAG_NAME"' in formula["run"]
    formula_upload = next(
        step
        for step in verify["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
        and step.get("with", {}).get("name") == "homebrew-formula"
    )
    assert formula_upload["if"] == formula["if"]
    assert formula_upload["with"]["path"] == ".release/homebrew/Formula/crewplane.rb"
    assert formula_upload["with"]["include-hidden-files"] == "true"


def test_homebrew_release_uses_scoped_token() -> None:
    job = load_workflow("release.yml")["jobs"]["homebrew-pr"]
    assert set(job["needs"]) == {"verify", "github-release"}
    assert job["if"] == "needs.verify.outputs.homebrew_eligible == 'true'"
    assert job["permissions"] == {"contents": "read"}
    assert job["env"]["SOURCE_COMMIT"] == "${{ needs.verify.outputs.release_commit }}"
    steps = job["steps"]
    token = next(step for step in steps if step.get("id") == "tap-token")
    assert token["uses"].startswith("actions/create-github-app-token@")
    assert token["with"] == {
        "client-id": "${{ vars.HOMEBREW_UPDATER_CLIENT_ID }}",
        "private-key": "${{ secrets.HOMEBREW_UPDATER_PRIVATE_KEY }}",
        "owner": "crewplaneai",
        "repositories": "homebrew-crewplane",
        "permission-contents": "write",
        "permission-pull-requests": "write",
    }
    checkout = next(
        step
        for step in steps
        if step.get("uses", "").startswith("actions/checkout@")
        and step.get("with", {}).get("repository") == "crewplaneai/homebrew-crewplane"
    )
    assert checkout["with"]["token"] == "${{ steps.tap-token.outputs.token }}"
    assert checkout["with"]["persist-credentials"] == "false"


def test_homebrew_release_consumes_verified_artifacts_and_source() -> None:
    job = load_workflow("release.yml")["jobs"]["homebrew-pr"]
    steps = job["steps"]
    downloads = {
        step["with"]["name"]: step["with"]["path"]
        for step in steps
        if step.get("uses", "").startswith("actions/download-artifact@")
    }
    assert downloads["release-bundle"] == "."
    assert downloads["homebrew-formula"] == ".release/homebrew/Formula"
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "gh auth setup-git" in commands
    publish = next(
        step
        for step in steps
        if "python scripts/release.py publish-homebrew-pr" in step.get("run", "")
    )
    assert '--expected-tag "$TAG_NAME"' in publish["run"]
    assert '--source-commit "$SOURCE_COMMIT"' in publish["run"]
    assert "--execute" in publish["run"]
