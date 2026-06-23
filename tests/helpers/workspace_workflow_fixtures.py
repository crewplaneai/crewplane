from __future__ import annotations

import json
import subprocess
from pathlib import Path

from crewplane.core.review_contract import ParsedReviewResult
from crewplane.runtime.execution.consensus import render_review_contract

BASE_APP_TEXT = "base application\n"
CANDIDATE_ROUND_1_APP_TEXT = "candidate round 1 application\n"
CANDIDATE_ROUND_2_APP_TEXT = "candidate round 2 application\n"


def write_initial_failure_fixtures(fixtures_dir: Path) -> None:
    write_fixture(
        fixtures_dir,
        "snapshot.read",
        "alpha_executor_0_round1.md",
        "Snapshot read complete.\n",
    )
    write_fixture(
        fixtures_dir,
        "implement.review",
        "alpha_executor_0_round1.md",
        "   \n",
    )
    write_fixture(
        fixtures_dir,
        "lineage.consumer",
        "alpha_executor_0_round1.md",
        "Lineage consumed.\n",
    )


def write_success_fixtures(fixtures_dir: Path) -> None:
    write_initial_failure_fixtures(fixtures_dir)
    write_fixture(
        fixtures_dir,
        "implement.review",
        "alpha_executor_0_round1.md",
        "# Candidate Round 1\n\nInitial implementation. Updated `src/app.txt`.\n",
        sidecar={
            "forbidden_prompt_contains": [CANDIDATE_ROUND_1_APP_TEXT],
            "required_prompt_contains": [BASE_APP_TEXT],
            "workspace_mutations": [
                {
                    "path": "src/app.txt",
                    "content": CANDIDATE_ROUND_1_APP_TEXT,
                }
            ],
        },
    )
    write_fixture(
        fixtures_dir,
        "implement.review",
        "alpha_reviewer_0_round1.md",
        review_output("CHANGES_REQUESTED", "- Address the missing handoff."),
        sidecar={
            "forbidden_prompt_contains": [BASE_APP_TEXT],
            "required_prompt_contains": [CANDIDATE_ROUND_1_APP_TEXT],
        },
    )
    write_fixture(
        fixtures_dir,
        "implement.review",
        "alpha_executor_0_round2.md",
        "# Candidate Round 2\n\nHandoff addressed. Updated `src/app.txt`.\n",
        sidecar={
            "forbidden_prompt_contains": [BASE_APP_TEXT],
            "required_prompt_contains": [CANDIDATE_ROUND_1_APP_TEXT],
            "workspace_mutations": [
                {
                    "path": "src/app.txt",
                    "content": CANDIDATE_ROUND_2_APP_TEXT,
                }
            ],
        },
    )
    write_fixture(
        fixtures_dir,
        "implement.review",
        "alpha_reviewer_0_round2.md",
        review_output("NO_FINDINGS", "None"),
        sidecar={
            "forbidden_prompt_contains": [BASE_APP_TEXT],
            "required_prompt_contains": [CANDIDATE_ROUND_2_APP_TEXT],
        },
    )
    write_fixture(
        fixtures_dir,
        "lineage.consumer",
        "alpha_executor_0_round1.md",
        "Lineage consumed. Updated `src/app.txt`.\n",
        sidecar={
            "forbidden_prompt_contains": [BASE_APP_TEXT],
            "required_prompt_contains": [CANDIDATE_ROUND_2_APP_TEXT],
        },
    )


def write_fixture(
    fixtures_dir: Path,
    node_id: str,
    filename: str,
    content: str,
    sidecar: dict[str, object] | None = None,
) -> None:
    path = fixtures_dir / node_id / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if sidecar is not None:
        path.with_suffix(".mutations.json").write_text(
            json.dumps(sidecar, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    else:
        path.with_suffix(".mutations.json").unlink(missing_ok=True)


def review_output(verdict: str, major: str) -> str:
    return render_review_contract(
        ParsedReviewResult(
            verdict=verdict,
            major_issues=major,
            minor_issues="None",
            nitpicks="None",
        )
    )


def assert_workspace_e2e_artifacts(run_dir: Path) -> None:
    manifest = read_json(run_dir / "manifests" / "run.json")
    assert manifest["resumed_nodes"] == ["requirements", "snapshot.read"]
    assert not (run_dir / "requirements" / "workspace-state.json").exists()

    snapshot_state = read_json(run_dir / "snapshot.read" / "workspace-state.json")
    assert snapshot_state["workspace"]["materialization"] == "snapshot_checkout"
    assert snapshot_state["invoker"]["implementation"] == "mock"

    review_status = read_json(
        run_dir / "implement.review" / "review-state" / "review-loop-status.json"
    )
    assert review_status["artifact_drift_warning_count"] == 0

    review_states = workspace_states(run_dir / "implement.review")
    assert all(state["invoker"]["implementation"] == "mock" for state in review_states)
    assert any(state["role"] == "reviewer" for state in review_states)
    executor_states = [
        state
        for state in review_states
        if state["role"] == "executor"
        and state["workspace"]["materialization"] == "worktree_checkout"
    ]
    assert executor_states
    assert any("bundle" in state for state in executor_states)
    final_executor_state = max(
        executor_states,
        key=lambda state: int(state["round_num"]),
    )
    result = final_executor_state["result"]
    assert isinstance(result, dict)
    result_commit = result["result_commit"]
    assert isinstance(result_commit, str)
    project_root = run_dir.parents[2]
    assert (
        git_text(project_root, "cat-file", "-p", f"{result_commit}:src/app.txt")
        == CANDIDATE_ROUND_2_APP_TEXT.strip()
    )

    consumer_states = workspace_states(run_dir / "lineage.consumer")
    assert any(
        state["source"]["node_id"] == "implement.review"
        and state["workspace"]["materialization"] == "worktree_checkout"
        for state in consumer_states
    )
    assert any(
        state.get("reuse", {}).get("strategy") == "fresh_checkout"
        and state.get("reuse", {}).get("reused") is False
        for state in consumer_states
    )
    results_dir = run_dir.parents[1] / "execution-results" / run_dir.name
    implement_result = (results_dir / "implement.review-result.md").read_text(
        encoding="utf-8"
    )
    consumer_result = (results_dir / "lineage.consumer-result.md").read_text(
        encoding="utf-8"
    )
    assert "Updated `src/app.txt`." in implement_result
    assert "Updated `src/app.txt`." in consumer_result
    assert "## Generated Files" in implement_result
    assert "[alpha_executor_0/src/app.txt]" in implement_result
    assert "## Generated Files" not in consumer_result
    assert (
        results_dir
        / "generated-files"
        / "implement.review"
        / "alpha_executor_0"
        / "src"
        / "app.txt"
    ).read_text(encoding="utf-8") == CANDIDATE_ROUND_2_APP_TEXT
    assert not (
        results_dir
        / "generated-files"
        / "lineage.consumer"
        / "alpha_executor_0"
        / "src"
        / "app.txt"
    ).exists()

    plan = read_json(run_dir / "preflight" / "execution-plan.json")
    locators = plan["workspace_file_locators"]
    assert any(locator["target"] == "reviewer_prompt" for locator in locators)
    assert any(
        locator["runtime_dynamic_after_candidate"] is True for locator in locators
    )


def workspace_states(stage_dir: Path) -> list[dict[str, object]]:
    return [read_json(path) for path in sorted(stage_dir.glob("workspace-state*.json"))]


def latest_succeeded_run(project_root: Path) -> Path:
    succeeded = []
    for run_dir in run_dirs(project_root):
        manifest_path = run_dir / "manifests" / "run.json"
        if not manifest_path.exists():
            continue
        manifest = read_json(manifest_path)
        if manifest.get("status") == "succeeded":
            succeeded.append(run_dir)
    assert succeeded
    return succeeded[-1]


def run_dirs(project_root: Path) -> list[Path]:
    stages_root = project_root / ".crewplane" / "execution-stages"
    if not stages_root.exists():
        return []
    return sorted(path for path in stages_root.iterdir() if path.is_dir())


def read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def git_text(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", root.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8").strip()
