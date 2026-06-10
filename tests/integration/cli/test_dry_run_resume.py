from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.resume import make_node_state, write_node_state, write_result
from tests.integration.cli.dry_run_helpers import (
    DryRunUnavailableArtifactsAdapter,
    artifact_tree,
    compile_preview,
    run_dry_run,
    write_nonfilesystem_config,
    write_run_history,
    write_sensitive_env_workflow,
    write_standard_project,
)


class CliDryRunResumeAdvisoryTests(unittest.TestCase):
    def test_dry_run_advises_full_run_without_creating_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(tmp_path)
            before = artifact_tree(tmp_path / ".orchestrator")

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("Resume advisory: would_execute_full_run", output_text)
            self.assertEqual(artifact_tree(tmp_path / ".orchestrator"), before)
            self.assertFalse(
                (tmp_path / ".orchestrator" / "preflight" / "fingerprint.key").exists()
            )

    def test_dry_run_advises_skip_for_valid_success_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(tmp_path)
            preview = compile_preview(tmp_path, config_path, workflow_path)
            write_run_history(
                tmp_path,
                preview,
                workflow_path,
                run_id="success-run",
                status="succeeded",
            )
            before = artifact_tree(tmp_path / ".orchestrator")

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("Resume advisory: would_skip", output_text)
            self.assertEqual(artifact_tree(tmp_path / ".orchestrator"), before)

    def test_dry_run_force_advises_full_run_despite_success_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(tmp_path)
            preview = compile_preview(tmp_path, config_path, workflow_path)
            write_run_history(
                tmp_path,
                preview,
                workflow_path,
                run_id="success-run",
                status="succeeded",
            )
            before = artifact_tree(tmp_path / ".orchestrator")

            output_text = run_dry_run(
                tmp_path,
                config_path,
                workflow_path,
                force=True,
            )

            self.assertIn("Resume advisory: would_execute_full_run", output_text)
            self.assertNotIn("Resume advisory: would_skip", output_text)
            self.assertEqual(artifact_tree(tmp_path / ".orchestrator"), before)

    def test_dry_run_advises_resume_for_valid_failed_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(tmp_path)
            preview = compile_preview(tmp_path, config_path, workflow_path)
            manifest = write_run_history(
                tmp_path,
                preview,
                workflow_path,
                run_id="failed-run",
                status="failed",
            )
            node = preview.nodes[0]
            result_descriptor = write_result(
                tmp_path
                / ".orchestrator"
                / "execution-results"
                / manifest.run_key_name,
                node.artifact_contract.output_path,
                "completed node output",
            )
            write_node_state(
                tmp_path / ".orchestrator" / "execution-stages" / manifest.run_key_name,
                make_node_state(manifest, node.id, [result_descriptor]),
            )
            before = artifact_tree(tmp_path / ".orchestrator")

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn(
                "Resume advisory: would_resume 1 node(s) from failed-run",
                output_text,
            )
            self.assertEqual(artifact_tree(tmp_path / ".orchestrator"), before)

    def test_dry_run_reports_resume_unavailable_for_non_filesystem_backend(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                config_writer=write_nonfilesystem_config,
            )
            DryRunUnavailableArtifactsAdapter.create_store_calls = 0
            before = artifact_tree(tmp_path / ".orchestrator")

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn(
                "Resume advisory: unavailable for non-filesystem artifact backends.",
                output_text,
            )
            self.assertEqual(DryRunUnavailableArtifactsAdapter.create_store_calls, 0)
            self.assertEqual(artifact_tree(tmp_path / ".orchestrator"), before)

    def test_dry_run_labels_ephemeral_fingerprint_decision_non_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                workflow_writer=write_sensitive_env_workflow,
            )
            before = artifact_tree(tmp_path / ".orchestrator")

            with patch.dict(os.environ, {"API_TOKEN": "super-secret"}):
                output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("Resume advisory: would_execute_full_run", output_text)
            self.assertIn(
                "Resume advisory: non-binding because sensitive fingerprints are "
                "ephemeral.",
                output_text,
            )
            self.assertEqual(artifact_tree(tmp_path / ".orchestrator"), before)
            self.assertFalse(
                (tmp_path / ".orchestrator" / "preflight" / "fingerprint.key").exists()
            )
