from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crewplane.architecture.contracts import CanonicalIntegrationConfig
from crewplane.architecture.errors import AdapterContractError
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_node_state, write_node_state, write_result
from tests.helpers.terminal_results import RESULT_SOURCE_TOKEN, write_result_source
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


def _write_terminal_history_workflow(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Task",
                "nodes:",
                "  - id: prior-result",
                "    mode: input",
                f'    source: "{RESULT_SOURCE_TOKEN}"',
                "---",
            ]
        ),
        encoding="utf-8",
    )


def _write_terminal_history_prompt_workflow(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Task",
                "nodes:",
                "  - id: review",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## review",
                "",
                f"Review {RESULT_SOURCE_TOKEN}.",
            ]
        ),
        encoding="utf-8",
    )


class InvalidTerminalHistoryArtifactsAdapter(DryRunUnavailableArtifactsAdapter):
    create_terminal_history_reader = None


class OptionsRequiredTerminalHistoryArtifactsAdapter(DryRunUnavailableArtifactsAdapter):
    received_options: dict[str, object] | None = None

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: dict[str, object] | None = None,
    ) -> CanonicalIntegrationConfig:
        del options
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options={"history_root": "canonical-history"},
            option_scopes={"history_root": "artifact"},
        )

    def create_terminal_history_reader(
        self,
        state_dir: Path,
        options: dict[str, object] | None = None,
    ) -> object:
        if options != {"history_root": "canonical-history"}:
            raise RuntimeError("canonical artifact options were not supplied")
        type(self).received_options = dict(options)
        return super().create_terminal_history_reader(state_dir, options)


def _write_invalid_terminal_history_adapter_config(path: Path) -> None:
    write_nonfilesystem_config(
        path,
        f"{__name__}:InvalidTerminalHistoryArtifactsAdapter",
    )


def _write_options_required_terminal_history_adapter_config(path: Path) -> None:
    write_nonfilesystem_config(
        path,
        f"{__name__}:OptionsRequiredTerminalHistoryArtifactsAdapter",
    )


class CliDryRunResumeAdvisoryTests(unittest.TestCase):
    def test_terminal_history_reader_receives_canonical_artifact_options(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                config_writer=(_write_options_required_terminal_history_adapter_config),
            )
            OptionsRequiredTerminalHistoryArtifactsAdapter.received_options = None

            compile_preview(tmp_path, config_path, workflow_path)

            self.assertEqual(
                OptionsRequiredTerminalHistoryArtifactsAdapter.received_options,
                {"history_root": "canonical-history"},
            )

    def test_present_invalid_history_capability_fails_contract_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                config_writer=_write_invalid_terminal_history_adapter_config,
            )

            with self.assertRaisesRegex(
                AdapterContractError,
                "create_terminal_history_reader",
            ):
                compile_preview(tmp_path, config_path, workflow_path)

    def test_external_adapter_with_history_reader_reads_local_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                config_writer=write_nonfilesystem_config,
                workflow_writer=_write_terminal_history_workflow,
            )
            write_result_source(tmp_path)

            preview = compile_preview(tmp_path, config_path, workflow_path)

            assert preview.static_file_payloads
            self.assertIn(b"prior result", preview.static_file_payloads.values())

    def test_provider_prompt_reads_terminal_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                config_writer=write_nonfilesystem_config,
                workflow_writer=_write_terminal_history_prompt_workflow,
            )
            write_result_source(tmp_path)

            preview = compile_preview(tmp_path, config_path, workflow_path)

            assert preview.static_file_payloads
            self.assertIn(b"prior result", preview.static_file_payloads.values())

    def test_dry_run_advises_full_run_without_creating_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(tmp_path)
            before = artifact_tree(tmp_path / ".crewplane")

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("Resume advisory: would_execute_full_run", output_text)
            self.assertEqual(artifact_tree(tmp_path / ".crewplane"), before)
            self.assertFalse(
                (tmp_path / ".crewplane" / "preflight" / "fingerprint.key").exists()
            )

    def test_dry_run_advises_skip_for_valid_success_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(tmp_path)
            preview = compile_preview(tmp_path, config_path, workflow_path)
            manifest = write_run_history(
                tmp_path,
                preview,
                workflow_path,
                run_id="success-run",
                status="succeeded",
            )
            node = preview.nodes[0]
            result_descriptor = write_result(
                tmp_path / ".crewplane" / "execution-results" / manifest.run_key_name,
                node.artifact_contract.output_path,
                "completed node output",
            )
            write_node_state(
                tmp_path / ".crewplane" / "execution-stages" / manifest.run_key_name,
                make_node_state(manifest, node.id, [result_descriptor]),
            )
            before = artifact_tree(tmp_path / ".crewplane")

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("Resume advisory: would_skip", output_text)
            self.assertEqual(artifact_tree(tmp_path / ".crewplane"), before)

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
            before = artifact_tree(tmp_path / ".crewplane")

            output_text = run_dry_run(
                tmp_path,
                config_path,
                workflow_path,
                force=True,
            )

            self.assertIn("Resume advisory: would_execute_full_run", output_text)
            self.assertNotIn("Resume advisory: would_skip", output_text)
            self.assertEqual(artifact_tree(tmp_path / ".crewplane"), before)

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
                tmp_path / ".crewplane" / "execution-results" / manifest.run_key_name,
                node.artifact_contract.output_path,
                "completed node output",
            )
            write_node_state(
                tmp_path / ".crewplane" / "execution-stages" / manifest.run_key_name,
                make_node_state(manifest, node.id, [result_descriptor]),
            )
            before = artifact_tree(tmp_path / ".crewplane")

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn(
                "Resume advisory: would_resume 1 node(s) from failed-run",
                output_text,
            )
            self.assertEqual(artifact_tree(tmp_path / ".crewplane"), before)

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
            before = artifact_tree(tmp_path / ".crewplane")

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn(
                "Resume advisory: unavailable for non-filesystem artifact backends.",
                output_text,
            )
            self.assertEqual(DryRunUnavailableArtifactsAdapter.create_store_calls, 0)
            self.assertEqual(artifact_tree(tmp_path / ".crewplane"), before)

    def test_dry_run_labels_ephemeral_fingerprint_decision_non_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                workflow_writer=write_sensitive_env_workflow,
            )
            before = artifact_tree(tmp_path / ".crewplane")

            with patch.dict(os.environ, {"API_TOKEN": "super-secret"}):
                output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("Resume advisory: would_execute_full_run", output_text)
            self.assertIn(
                "Resume advisory: non-binding because sensitive fingerprints are "
                "ephemeral.",
                output_text,
            )
            self.assertEqual(artifact_tree(tmp_path / ".crewplane"), before)
            self.assertFalse(
                (tmp_path / ".crewplane" / "preflight" / "fingerprint.key").exists()
            )
