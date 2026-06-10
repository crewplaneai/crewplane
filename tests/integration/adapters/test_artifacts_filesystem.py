import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from orchestrator_cli.adapters.artifacts.filesystem import FilesystemArtifactsAdapter
from orchestrator_cli.core.execution_state import RUN_STATE_SCHEMA_VERSION, RunManifest
from orchestrator_cli.version import SCHEMA_VERSION


class FilesystemArtifactsAdapterTests(unittest.TestCase):
    def test_create_store_builds_output_manager(self) -> None:
        adapter = FilesystemArtifactsAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            store = adapter.create_store(
                workflow_name="Workflow",
                orchestrator_dir=tmp_path,
                project_root=tmp_path,
                options={
                    "log_cli_output": True,
                    "allowed_template_paths": [],
                },
            )
            stage_dir = store.create_stage_dir("build.node")
            (stage_dir / "task_round1.md").write_text("node content", encoding="utf-8")
            store.finalize_stage("build.node")
            result_text = store.get_stage_output_path("build.node").read_text(
                encoding="utf-8"
            )
        self.assertEqual(store.task_name, "workflow")
        self.assertIn("node content", result_text)

    def test_create_store_rejects_invalid_allowed_paths_option(self) -> None:
        adapter = FilesystemArtifactsAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with self.assertRaisesRegex(ValueError, "allowed_template_paths"):
                adapter.create_store(
                    workflow_name="Workflow",
                    orchestrator_dir=tmp_path,
                    project_root=tmp_path,
                    options={"allowed_template_paths": "bad"},
                )

    def test_canonicalize_options_does_not_create_run_dirs(self) -> None:
        adapter = FilesystemArtifactsAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = adapter.canonicalize_options(
                implementation="filesystem",
                resolved_identity="orchestrator_cli.adapters.artifacts.filesystem:FilesystemArtifactsAdapter",
                options={"allowed_template_paths": [], "log_cli_output": True},
            )

            self.assertEqual(config.option_scopes["allowed_template_paths"], "artifact")
            self.assertEqual(config.option_scopes["log_cli_output"], "artifact")
            self.assertFalse((tmp_path / "execution-stages").exists())
            self.assertFalse((tmp_path / "execution-results").exists())

    def test_store_writes_current_run_manifest(self) -> None:
        adapter = FilesystemArtifactsAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            store = adapter.create_store(
                workflow_name="Workflow",
                orchestrator_dir=tmp_path,
                project_root=tmp_path,
                options={"allowed_template_paths": [], "log_cli_output": True},
            )
            manifest = RunManifest(
                run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
                plan_schema_version=SCHEMA_VERSION,
                workflow_identity=".orchestrator/workflows/workflow.task.md",
                workflow_name="Workflow",
                workflow_signature="0" * 64,
                run_id=store.run_id,
                run_key_name=store.run_key_name,
                started_at=datetime(2026, 6, 3, 12, 0).isoformat(),
                status="running",
                effective_runtime_config_signature="1" * 64,
                preflight_plan_path="preflight/execution-plan.json",
                preflight_manifest_path="preflight/manifest.json",
                runtime_config_snapshot_path="preflight/runtime-config-snapshot.json",
                runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
                workflow_source="workflow source",
                composed_workflow={"schema_version": SCHEMA_VERSION},
            )

            manifest_path = store.write_run_manifest(manifest)

            self.assertEqual(manifest_path.name, "run.json")
            self.assertEqual(manifest_path.parent.name, "manifests")
