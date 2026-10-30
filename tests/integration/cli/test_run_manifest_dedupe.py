import io
import json
import re
import unittest
from pathlib import Path

import pytest
import yaml

import crewplane.cli.app as cli
from tests.helpers import mock_resume
from tests.helpers.foreach import (
    copy_foreach_fixture,
    workflow_document,
    write_workflow_document,
)
from tests.helpers.working_directory import temporary_project_cwd
from tests.integration.cli.cli_workflow_helpers import (
    cli_process_state,
    mock_invocations,
    run_manifests,
    write_basic_config,
    write_basic_workflow,
    write_mock_config,
    write_successful_workflow_outputs,
    write_workflow_with_name,
)
from tests.integration.cli.workflow_runner_support import (
    result_directories,
    run_directories,
)


class CliRunManifestDedupeTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def temporary_directory_root(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def test_run_skips_duplicate_context_without_force(self) -> None:
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
                    force=False,
                )

            self.assertEqual(calls["count"], 1)
            self.assertIn("Identical context detected", stream.getvalue())
            stage_runs = sorted(
                (tmp_path / ".crewplane" / "execution-stages").glob("task-*")
            )
            result_runs = sorted(
                (tmp_path / ".crewplane" / "execution-results").glob("task-*")
            )
            self.assertEqual(len(stage_runs), 1)
            self.assertEqual(len(result_runs), 1)

    def test_run_prints_artifact_paths_with_sanitized_workflow_key(self) -> None:
        with temporary_project_cwd(self.tmp_path) as tmp_path:
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_workflow_with_name(workflow_path, "Review apps")

            stream = io.StringIO()

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                return plan

            with cli_process_state(stream) as process_state:
                process_state.setattr(cli, "execute_workflow", fake_execute_workflow)
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )

            output_text = stream.getvalue().replace("\\", "/")
            self.assertIn("Workflow: Review apps", output_text)
            self.assertIn("Artifact key: review-apps", output_text)
            stages_root = (
                (tmp_path / ".crewplane" / "execution-stages").resolve().as_posix()
            )
            results_root = (
                (tmp_path / ".crewplane" / "execution-results").resolve().as_posix()
            )
            self.assertRegex(
                output_text,
                re.escape(stages_root)
                + r"/review-apps--[0-9a-f]{12}-\d{8}-\d{6}(?:-\d{6})?",
            )
            self.assertRegex(
                output_text,
                re.escape(results_root)
                + r"/review-apps--[0-9a-f]{12}-\d{8}-\d{6}(?:-\d{6})?",
            )


def test_run_force_executes_duplicate_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.yml"
    workflow_path = tmp_path / "workflow.task.md"
    write_mock_config(config_path)
    write_basic_workflow(workflow_path)
    signatures = []
    for force, expected_count in [(False, 1), (False, 1), (True, 2)]:
        cli.run(
            tasks_file=workflow_path, config_file=config_path, no_live=True, force=force
        )
        assert len(mock_invocations(tmp_path)) == expected_count
        assert len(run_directories(tmp_path)) == expected_count
        results = result_directories(tmp_path)
        assert len(results) == expected_count
        assert all(
            "Do review." in (run / "review.node-result.md").read_text()
            for run in results
        )
        manifests = run_manifests(tmp_path)
        assert all(manifest["status"] == "succeeded" for manifest in manifests)
        signatures.extend(manifest["workflow_signature"] for manifest in manifests)
    assert len(set(signatures)) == 1


def test_dynamic_collection_cap_controls_dedupe_and_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    workflow = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(workflow)
    fixtures = tmp_path / "outputs"
    config = tmp_path / ".crewplane/config.yml"
    mock_resume.write_mock_config(config, fixtures)
    settings = yaml.safe_load(config.read_bytes())
    settings["settings"]["max_concurrent_nodes"] = 1
    config.write_text(yaml.safe_dump(settings), encoding="utf-8", newline="\n")
    mock_resume.write_executor_fixture(
        fixtures, "planner", '<!-- findings -->[{"chunk":"a"}]<!-- /findings -->'
    )
    mock_resume.write_executor_fixture(
        fixtures,
        "chunks.i0.part.analyze",
        "<!-- findings -->analysis<!-- /findings -->",
    )
    mock_resume.write_executor_fixture(fixtures, "summarize", "Summary")
    signatures = {}
    for limit, force, expected_runs in [
        (4, False, 1),
        (4, False, 1),
        (2, False, 2),
        (2, False, 2),
        (2, True, 3),
    ]:
        payload["nodes"][2]["max_items"] = limit
        write_workflow_document(workflow, payload, body)
        constraint = f"Executor findings: JSON array with at most {limit} items."
        (fixtures / "planner/alpha_executor_0_round1.mutations.json").write_text(
            json.dumps({"required_prompt_contains": [constraint]}),
            encoding="utf-8",
            newline="\n",
        )
        cli.run(tasks_file=workflow, config_file=config, no_live=True, force=force)
        runs = run_directories(tmp_path)
        assert len(runs) == expected_runs
        manifests = run_manifests(tmp_path)
        assert all(manifest["status"] == "succeeded" for manifest in manifests)
        current = manifests[-1]
        assert current["resumed_nodes"] == []
        signatures.setdefault(limit, current["workflow_signature"])
        assert current["workflow_signature"] == signatures[limit]
        plan = json.loads((runs[-1] / "preflight/execution-plan.json").read_bytes())
        renders = json.loads((runs[-1] / "preflight/render-plans.json").read_bytes())
        assert renders == plan["render_plans"]
        producer = next(render for render in renders if render["node_id"] == "planner")
        assert all(
            constraint in stream["fragments"][-1]["text"]
            for stream in producer["streams"]
        )
        results = tmp_path / ".crewplane/execution-results" / runs[-1].name
        assert json.loads((results / "planner-findings.md").read_bytes()) == [
            {"chunk": "a"}
        ]
        assert json.loads((results / "chunks-result.md").read_bytes())[0]["item"] == {
            "chunk": "a"
        }
        expansion = runs[-1] / "preflight/expansions/chunks"
        assert (expansion / "expansion.json").is_file()
        assert json.loads((expansion / "items/0.json").read_bytes()) == {"chunk": "a"}
    assert signatures[4] != signatures[2]
