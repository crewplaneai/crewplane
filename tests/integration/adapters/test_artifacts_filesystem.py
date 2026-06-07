import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.adapters.artifacts.filesystem import FilesystemArtifactsAdapter


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

    def test_workflow_signature_exists_does_not_create_run_dirs(self) -> None:
        adapter = FilesystemArtifactsAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            exists = adapter.workflow_signature_exists(
                workflow_name="Workflow",
                orchestrator_dir=tmp_path,
                options={"allowed_template_paths": [], "log_cli_output": True},
                workflow_signature="a" * 64,
            )

            self.assertFalse(exists)
            self.assertFalse((tmp_path / "execution-stages").exists())
            self.assertFalse((tmp_path / "execution-results").exists())

    def test_workflow_signature_exists_rejects_invalid_options(self) -> None:
        adapter = FilesystemArtifactsAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with self.assertRaisesRegex(ValueError, "allowed_template_paths"):
                adapter.workflow_signature_exists(
                    workflow_name="Workflow",
                    orchestrator_dir=tmp_path,
                    options={"allowed_template_paths": "bad"},
                    workflow_signature="a" * 64,
                )
