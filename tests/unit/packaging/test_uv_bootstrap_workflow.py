import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.unit.packaging.release_surfaces_support import read_text, write_executable
from tests.unit.packaging.test_release_ci_workflows import workflow_step_run


def test_uv_update_follows_pull_request_ci_without_requiring_success() -> None:
    workflow = yaml.load(
        read_text(".github", "workflows", "ci-tooling-update.yml"),
        Loader=yaml.BaseLoader,
    )
    assert workflow["on"]["workflow_run"] == {
        "workflows": ["ci"],
        "types": ["completed"],
        "branches": ["dependabot/**"],
    }
    assert "schedule" in workflow["on"]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    assert workflow["concurrency"]["queue"] == "max"
    job = workflow["jobs"]["uv-bootstrap-update"]
    for condition in (
        "github.repository == 'crewplaneai/crewplane'",
        "github.ref == 'refs/heads/master'",
        "github.event_name != 'workflow_run'",
        "github.event.workflow_run.event == 'pull_request'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert condition in job["if"]
    assert "conclusion" not in job["if"]
    assert job["steps"][0]["with"]["ref"] == "master"
    selection = next(step for step in job["steps"] if step.get("id") == "lane")
    assert selection["env"]["TARGET_HEAD_REF"] == (
        "${{ github.event.workflow_run.head_branch }}"
    )
    dispatch = next(
        step
        for step in job["steps"]
        if step.get("name") == "Dispatch CI for the bot-authored push"
    )
    assert dispatch["if"] == "steps.publish.outputs.published == 'true'"


@pytest.mark.parametrize(
    ("target_head", "changes_pin", "expected_active"),
    [
        ("dependabot/uv/update", "true", True),
        ("dependabot/uv/update", "false", False),
        ("dependabot/github-actions/update", "true", False),
        ("", "true", True),
        ("", "false", False),
    ],
)
def test_uv_update_selects_only_the_triggering_branch(
    tmp_path: Path, target_head: str, changes_pin: str, expected_active: bool
) -> None:
    workflow = yaml.safe_load(
        read_text(".github", "workflows", "ci-tooling-update.yml")
    )
    job = workflow["jobs"]["uv-bootstrap-update"]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_executable(
        fake_bin / "gh",
        '#!/bin/sh\ncase "$2" in\n'
        'list) printf "56\\tdependabot/uv/update\\n" ;;\n'
        'view) printf "%s\\n" "$CHANGES_PIN" ;;\n'
        "*) exit 99 ;;\nesac\n",
    )
    write_executable(
        fake_bin / "git",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$GIT_LOG"\n',
    )
    output = tmp_path / "output"
    git_log = tmp_path / "git-log"
    result = subprocess.run(
        ["bash", "-c", workflow_step_run(job, "Select the Dependabot uv PR")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "TARGET_HEAD_REF": target_head,
            "CHANGES_PIN": changes_pin,
            "GIT_LOG": str(git_log),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert f"active={str(expected_active).lower()}\n" in output.read_text()
    if expected_active:
        assert "head_ref=dependabot/uv/update\n" in output.read_text()
        assert (
            "fetch --no-tags origin master dependabot/uv/update" in git_log.read_text()
        )
    else:
        assert "fetch" not in git_log.read_text()
