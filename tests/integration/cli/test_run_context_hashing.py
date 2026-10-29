import io
import unittest
from pathlib import Path

import pytest
import yaml

import crewplane.cli.app as cli
from tests.helpers.working_directory import temporary_project_cwd
from tests.integration.cli.cli_workflow_helpers import (
    cli_process_state,
    mock_invocations,
    run_manifests,
    write_basic_config,
    write_basic_workflow,
    write_mock_config,
    write_successful_workflow_outputs,
)
from tests.integration.cli.repeat_force_run_support import create_project
from tests.integration.cli.workflow_runner_support import (
    result_directories,
    run_directories,
)


def test_yaml_count_edits_preserve_run_history_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, None, node_count=1)
    project.workflow_path.unlink()
    project.workflow_path = tmp_path / "repeat.yaml"
    project.workflow["nodes"][0]["providers"] = [{"provider": "alpha"}]
    project.workflow["nodes"][0]["prompt_segments"] = [
        {"role": "shared", "content": "Scan."}
    ]
    for count in [None, 1, 2, None]:
        if count is None:
            project.workflow.pop("repeat_force_run_count", None)
        else:
            project.workflow["repeat_force_run_count"] = count
        project.workflow_path.write_text(
            yaml.safe_dump(project.workflow), encoding="utf-8"
        )
        result = project.run("--no-live")
        assert result.exit_code == 0, result.output
    records = project.manifests()
    assert len(records) == 4
    assert len({record["workflow_signature"] for record in records}) == 1
    assert [
        record["composed_workflow"].get("repeat_force_run_count") for record in records
    ] == [None, 1, 2, 2]
    assert "Identical context detected" in result.output


class CliRunContextHashingTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def temporary_directory_root(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def test_failed_run_writes_failure_manifest_and_does_not_trigger_skip(self) -> None:
        with temporary_project_cwd(self.tmp_path) as tmp_path:
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            calls = {"count": 0}

            async def flaky_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("simulated failure")
                return plan

            with cli_process_state(stream) as process_state:
                process_state.setattr(cli, "execute_workflow", flaky_execute_workflow)
                with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                        force=False,
                    )

                stage_runs = sorted(
                    (tmp_path / ".crewplane" / "execution-stages").glob("task-*")
                )
                self.assertEqual(len(stage_runs), 1)
                manifests_dir = stage_runs[0] / "manifests"
                # A failure manifest is now written so the run is recorded.
                self.assertTrue(manifests_dir.exists())
                manifest_files = list(manifests_dir.glob("*.json"))
                self.assertEqual(len(manifest_files), 1)
                import json as _json

                manifest_data = _json.loads(
                    manifest_files[0].read_text(encoding="utf-8")
                )
                self.assertEqual(manifest_data.get("status"), "failed")

                # The second run must still execute (failure manifest must not trigger skip).
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )

            self.assertEqual(calls["count"], 2)
            self.assertNotIn("Identical context detected", stream.getvalue())

    def test_run_skips_duplicate_context_across_previous_run_folders(self) -> None:
        with temporary_project_cwd(self.tmp_path) as tmp_path:
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            calls = {"count": 0}

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]
                calls["count"] += 1
                write_successful_workflow_outputs(
                    plan,
                    output,
                    kwargs["workflow_identity"],
                )
                return plan

            with cli_process_state(stream) as process_state:
                process_state.setattr(cli, "execute_workflow", fake_execute_workflow)
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=True,
                )
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )

            self.assertEqual(calls["count"], 2)
            self.assertIn("Identical context detected", stream.getvalue())
            stage_runs = sorted(
                (tmp_path / ".crewplane" / "execution-stages").glob("task-*")
            )
            self.assertEqual(len(stage_runs), 2)
            self.assertEqual(len(result_directories(tmp_path)), 2)


def test_run_reexecutes_when_env_template_value_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.yml"
    workflow_path = tmp_path / "workflow.task.md"
    write_mock_config(config_path)
    write_basic_workflow(workflow_path, "branch={{env:BRANCH_NAME}}")
    signatures = []
    for branch, expected_count in [
        ("feature/one", 1),
        ("feature/one", 1),
        ("feature/two", 2),
        ("feature/two", 2),
    ]:
        monkeypatch.setenv("BRANCH_NAME", branch)
        cli.run(tasks_file=workflow_path, config_file=config_path, no_live=True)
        assert len(mock_invocations(tmp_path)) == expected_count
        assert len(run_directories(tmp_path)) == expected_count
        assert len(result_directories(tmp_path)) == expected_count
        manifests = run_manifests(tmp_path)
        assert all(manifest["status"] == "succeeded" for manifest in manifests)
        signatures.append(manifests[-1]["workflow_signature"])
    assert signatures[0] == signatures[1]
    assert signatures[0] != signatures[2]
    assert signatures[2] == signatures[3]
    first, second = result_directories(tmp_path)
    assert "branch=feature/one" in (first / "review.node-result.md").read_text()
    assert "branch=feature/two" in (second / "review.node-result.md").read_text()
