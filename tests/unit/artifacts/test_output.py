from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.artifacts.generated_files import (
    generated_file_source_root,
    snapshot_generated_file_workspace,
)
from orchestrator_cli.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ArtifactDescriptor,
    NodeState,
    RunManifest,
)
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    ExecutionPolicy,
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from orchestrator_cli.version import SCHEMA_VERSION


def _workflow_signature(label: str) -> str:
    import hashlib

    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _minimal_plan(output: OutputManager) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        project_root=output.base_dir.as_posix(),
        context_root=output.stages_dir.as_posix(),
        manifest_root=(output.stages_dir / "manifests").as_posix(),
        created_at=datetime(2026, 6, 3).isoformat(),
        workflow_name="workflow",
        workflow_signature=_workflow_signature("workflow"),
        execution_order=["build.node"],
        nodes=[
            PreflightExecutionNode(
                id="build.node",
                mode="sequential",
                artifact_contract=ArtifactContract(output_path="build.node-result.md"),
                execution_policy=ExecutionPolicy(),
            )
        ],
        render_plans=[],
        static_resources=[],
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        effective_runtime_config_signature=_workflow_signature("runtime"),
        fingerprint_metadata={"payload_version": "1"},
    )


def _running_manifest(
    output: OutputManager,
    workflow_signature: str | None = None,
) -> RunManifest:
    return RunManifest(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=SCHEMA_VERSION,
        workflow_identity=".orchestrator/workflows/workflow.task.md",
        workflow_name="workflow",
        workflow_signature=workflow_signature or _workflow_signature("workflow"),
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        started_at=datetime(2026, 6, 3, 12, 0).isoformat(),
        status="running",
        effective_runtime_config_signature=_workflow_signature("runtime"),
        preflight_plan_path="preflight/execution-plan.json",
        preflight_manifest_path="preflight/manifest.json",
        runtime_config_snapshot_path="preflight/runtime-config-snapshot.json",
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        workflow_source="workflow source",
        composed_workflow={"schema_version": SCHEMA_VERSION, "name": "workflow"},
    )


class OutputManagerTests(unittest.TestCase):
    def test_run_allocation_does_not_create_results_until_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)

            self.assertTrue(output.stages_dir.exists())
            self.assertFalse((base_dir / "execution-results").exists())

            stage_dir = output.create_stage_dir("build.node")
            (stage_dir / "alpha_round1.md").write_text("alpha", encoding="utf-8")
            output.finalize_stage("build.node")

            self.assertTrue(output.results_dir.exists())

    def test_finalize_stage_consolidates_task_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager("Workflow", base_dir=Path(tmp_dir))
            stage_dir = output.create_stage_dir("build.node")
            (stage_dir / "alpha_round1.md").write_text("alpha", encoding="utf-8")
            (stage_dir / "beta_round1.md").write_text("beta", encoding="utf-8")

            result = output.finalize_stage("build.node")

            result_text = output.get_stage_output_path("build.node").read_text(
                encoding="utf-8"
            )
            self.assertEqual(result.stage_name, "build.node")
            self.assertIn("alpha", result_text)
            self.assertIn("beta", result_text)

    def test_finalize_stage_links_generated_files_against_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            (base_dir / "src").mkdir()
            (base_dir / "src" / "app.txt").write_text("content", encoding="utf-8")
            stage_dir = output.create_stage_dir("build.node")
            (stage_dir / "alpha_round1.md").write_text(
                "Updated `src/app.txt`.\n",
                encoding="utf-8",
            )

            output.finalize_stage("build.node")

            result_text = output.get_stage_output_path("build.node").read_text(
                encoding="utf-8"
            )
            self.assertIn("## Generated Files", result_text)
            self.assertIn("[src/app.txt]", result_text)

    def test_finalize_stage_namespaces_workspace_generated_files_by_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            alpha_workspace = base_dir / "alpha-workspace"
            beta_workspace = base_dir / "beta-workspace"
            (alpha_workspace / "src").mkdir(parents=True)
            (beta_workspace / "src").mkdir(parents=True)
            (alpha_workspace / "src" / "app.txt").write_text(
                "alpha content",
                encoding="utf-8",
            )
            (beta_workspace / "src" / "app.txt").write_text(
                "beta content",
                encoding="utf-8",
            )
            stage_dir = output.create_stage_dir("build.node")
            alpha_output = stage_dir / "alpha_round1.md"
            beta_output = stage_dir / "beta_round1.md"
            alpha_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")
            beta_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")

            result = output.finalize_stage(
                "build.node",
                generated_file_workspace_roots={
                    alpha_output.resolve(strict=False): alpha_workspace,
                    beta_output.resolve(strict=False): beta_workspace,
                },
            )

            result_text = output.get_stage_output_path("build.node").read_text(
                encoding="utf-8"
            )
            self.assertIn("[alpha/src/app.txt]", result_text)
            self.assertIn("[beta/src/app.txt]", result_text)
            self.assertEqual(len(result.generated_files), 2)
            generated_dir = output.results_dir / "generated-files" / "build.node"
            self.assertEqual(
                (generated_dir / "alpha" / "src" / "app.txt").read_text(
                    encoding="utf-8"
                ),
                "alpha content",
            )
            self.assertEqual(
                (generated_dir / "beta" / "src" / "app.txt").read_text(
                    encoding="utf-8"
                ),
                "beta content",
            )

    def test_workspace_generated_files_hash_truncated_stage_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            stage_prefix = "a" * 120
            generated_paths = []
            for suffix in ("x", "y"):
                stage_name = f"{stage_prefix}{suffix}"
                workspace = base_dir / f"workspace-{suffix}"
                (workspace / "src").mkdir(parents=True)
                (workspace / "src" / "app.txt").write_text(
                    f"{suffix} content",
                    encoding="utf-8",
                )
                stage_dir = output.create_stage_dir(stage_name)
                provider_output = stage_dir / "alpha_round1.md"
                provider_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")

                result = output.finalize_stage(
                    stage_name,
                    generated_file_workspace_roots={
                        provider_output.resolve(strict=False): workspace,
                    },
                )
                generated_paths.extend(result.generated_files)

            self.assertEqual(len(generated_paths), 2)
            self.assertNotEqual(generated_paths[0].parent, generated_paths[1].parent)
            self.assertEqual(
                generated_paths[0].read_text(encoding="utf-8"), "x content"
            )
            self.assertEqual(
                generated_paths[1].read_text(encoding="utf-8"), "y content"
            )

    def test_workspace_generated_files_use_snapshot_not_mutated_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            workspace = base_dir / "workspace"
            (workspace / "src").mkdir(parents=True)
            generated_file = workspace / "src" / "app.txt"
            generated_file.write_text("original", encoding="utf-8")
            stage_dir = output.create_stage_dir("build.node")
            alpha_output = stage_dir / "alpha_round1.md"
            alpha_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")
            snapshot = snapshot_generated_file_workspace(alpha_output, workspace)
            generated_file.write_text("mutated", encoding="utf-8")

            output.finalize_stage(
                "build.node",
                generated_file_workspace_roots={
                    alpha_output.resolve(strict=False): snapshot,
                },
            )

            generated_dir = output.results_dir / "generated-files" / "build.node"
            self.assertEqual(
                (generated_dir / "alpha" / "src" / "app.txt").read_text(
                    encoding="utf-8"
                ),
                "original",
            )

    def test_workspace_generated_files_resolve_original_absolute_paths_from_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            workspace = base_dir / "workspace"
            (workspace / "src").mkdir(parents=True)
            generated_file = workspace / "src" / "app.txt"
            generated_file.write_text("original", encoding="utf-8")
            stage_dir = output.create_stage_dir("build.node")
            alpha_output = stage_dir / "alpha_round1.md"
            alpha_output.write_text(
                f"Updated `{generated_file.as_posix()}`.\n",
                encoding="utf-8",
            )
            snapshot = snapshot_generated_file_workspace(alpha_output, workspace)
            shutil.rmtree(workspace)

            result = output.finalize_stage(
                "build.node",
                generated_file_workspace_roots={
                    alpha_output.resolve(strict=False): snapshot,
                },
            )

            generated_dir = output.results_dir / "generated-files" / "build.node"
            generated_result = generated_dir / "alpha" / "src" / "app.txt"
            self.assertEqual(
                generated_result.read_text(encoding="utf-8"),
                "original",
            )
            self.assertEqual(result.generated_files, (generated_result,))

    def test_workspace_generated_file_snapshot_skips_unchanged_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            workspace = base_dir / "workspace"
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "documented.txt").write_text(
                "same bytes",
                encoding="utf-8",
            )
            stage_dir = output.create_stage_dir("build.node")
            alpha_output = stage_dir / "alpha_round1.md"
            alpha_output.write_text(
                "Updated `src/documented.txt`.\n",
                encoding="utf-8",
            )

            snapshot = snapshot_generated_file_workspace(
                alpha_output,
                workspace,
                changed_paths=set(),
            )
            snapshot_metadata = json.loads(
                (snapshot / ".orchestrator-generated-file-snapshot.json").read_text(
                    encoding="utf-8"
                )
            )

            output.finalize_stage(
                "build.node",
                generated_file_workspace_roots={
                    alpha_output.resolve(strict=False): snapshot,
                },
            )

            result_text = output.get_stage_output_path("build.node").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("## Generated Files", result_text)
            self.assertEqual(
                snapshot_metadata,
                {"files": []},
            )
            self.assertFalse(
                (
                    output.results_dir
                    / "generated-files"
                    / "build.node"
                    / "alpha"
                    / "src"
                    / "documented.txt"
                ).exists()
            )

    def test_workspace_generated_file_snapshot_filters_before_size_limits(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            workspace = base_dir / "workspace"
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "created.txt").write_text("x", encoding="utf-8")
            (workspace / "src" / "unchanged.txt").write_text(
                "oversized unchanged",
                encoding="utf-8",
            )
            stage_dir = output.create_stage_dir("build.node")
            alpha_output = stage_dir / "alpha_round1.md"
            alpha_output.write_text(
                "\n".join(
                    [
                        "## Generated Files",
                        "",
                        "- `src/created.txt`",
                        "- `src/unchanged.txt`",
                    ]
                ),
                encoding="utf-8",
            )

            with patch(
                "orchestrator_cli.artifacts.generated_files."
                "MAX_GENERATED_FILE_SNAPSHOT_BYTES",
                1,
            ):
                snapshot = snapshot_generated_file_workspace(
                    alpha_output,
                    workspace,
                    changed_paths={"src/created.txt"},
                )
            snapshot_metadata = json.loads(
                (snapshot / ".orchestrator-generated-file-snapshot.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(
                snapshot_metadata,
                {
                    "files": [
                        {
                            "changed": True,
                            "path": "src/created.txt",
                            "size_bytes": 1,
                        }
                    ]
                },
            )
            self.assertTrue((snapshot / "src" / "created.txt").is_file())
            self.assertFalse((snapshot / "src" / "unchanged.txt").exists())

    def test_workspace_generated_file_snapshot_rejects_too_many_before_copying(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            workspace = base_dir / "workspace"
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "one.txt").write_text("1", encoding="utf-8")
            (workspace / "src" / "two.txt").write_text("2", encoding="utf-8")
            stage_dir = output.create_stage_dir("build.node")
            alpha_output = stage_dir / "alpha_round1.md"
            alpha_output.write_text(
                "\n".join(
                    [
                        "## Generated Files",
                        "",
                        "- `src/one.txt`",
                        "- `src/two.txt`",
                    ]
                ),
                encoding="utf-8",
            )
            snapshot_root = generated_file_source_root(alpha_output)

            with (
                patch(
                    "orchestrator_cli.artifacts.generated_files."
                    "MAX_GENERATED_FILE_SNAPSHOT_FILES",
                    1,
                ),
                self.assertRaisesRegex(RuntimeError, "too many files"),
            ):
                snapshot_generated_file_workspace(alpha_output, workspace)

            self.assertFalse(snapshot_root.exists())

    def test_workspace_generated_file_snapshot_rejects_size_change_during_copy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            workspace = base_dir / "workspace"
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "app.txt").write_text("x", encoding="utf-8")
            stage_dir = output.create_stage_dir("build.node")
            alpha_output = stage_dir / "alpha_round1.md"
            alpha_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")
            snapshot_root = generated_file_source_root(alpha_output)

            def write_expanded_copy(source: Path, target: Path) -> Path:
                target.write_bytes(source.read_bytes() + b"expanded")
                return target

            with (
                patch(
                    "orchestrator_cli.artifacts.generated_files.shutil.copyfile",
                    side_effect=write_expanded_copy,
                ),
                self.assertRaisesRegex(RuntimeError, "changed while copying"),
            ):
                snapshot_generated_file_workspace(alpha_output, workspace)

            self.assertFalse((snapshot_root / "src" / "app.txt").exists())
            self.assertFalse(
                (snapshot_root / ".orchestrator-generated-file-snapshot.json").exists()
            )

    def test_workspace_generated_file_snapshot_ignores_hardlinked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            workspace = base_dir / "workspace"
            (workspace / "src").mkdir(parents=True)
            outside_file = base_dir / "outside.txt"
            outside_file.write_text("external", encoding="utf-8")
            generated_file = workspace / "src" / "leak.txt"
            try:
                os.link(outside_file, generated_file)
            except OSError as exc:
                self.skipTest(f"hard links are unavailable: {exc}")
            stage_dir = output.create_stage_dir("build.node")
            alpha_output = stage_dir / "alpha_round1.md"
            alpha_output.write_text("Updated `src/leak.txt`.\n", encoding="utf-8")

            snapshot = snapshot_generated_file_workspace(alpha_output, workspace)

            self.assertFalse((snapshot / "src" / "leak.txt").exists())

    def test_generated_file_snapshot_rejects_symlinked_source_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            workspace = base_dir / "workspace"
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "app.txt").write_text("content", encoding="utf-8")
            stage_dir = output.create_stage_dir("build.node")
            outside = base_dir / "outside"
            outside.mkdir()
            (stage_dir / "generated-file-sources").symlink_to(
                outside,
                target_is_directory=True,
            )
            alpha_output = stage_dir / "alpha_round1.md"
            alpha_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                snapshot_generated_file_workspace(
                    alpha_output,
                    workspace,
                    changed_paths={"src/app.txt"},
                )

            self.assertTrue(outside.exists())

    def test_finalize_stage_can_disable_generated_file_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            (base_dir / "src").mkdir()
            (base_dir / "src" / "app.txt").write_text("stale", encoding="utf-8")
            stage_dir = output.create_stage_dir("build.node")
            (stage_dir / "alpha_round1.md").write_text(
                "Updated `src/app.txt`.\n",
                encoding="utf-8",
            )

            output.finalize_stage(
                "build.node",
                generated_file_detection_enabled=False,
            )

            result_text = output.get_stage_output_path("build.node").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("## Generated Files", result_text)
            self.assertIn("Updated `src/app.txt`.", result_text)

    def test_stage_names_do_not_escape_or_collide_after_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager("Workflow", base_dir=Path(tmp_dir))

            escaped_candidate = output.create_stage_dir("..-")
            dashed = output.create_stage_dir("-a")
            plain = output.create_stage_dir("a")

            self.assertTrue(
                escaped_candidate.resolve().is_relative_to(output.stages_dir)
            )
            self.assertNotEqual(dashed, plain)
            self.assertNotEqual(
                output.get_stage_output_path("-a"), output.get_stage_output_path("a")
            )

    def test_write_and_update_run_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            output = OutputManager("Workflow", base_dir=base_dir)
            manifest = _running_manifest(output)

            output.write_run_manifest(manifest)
            output.update_run_manifest_status(
                "succeeded",
                datetime(2026, 6, 3, 12, 1).isoformat(),
            )

            manifest_path = output.stages_dir / "manifests" / "run.json"
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(payload["workflow_signature"], manifest.workflow_signature)
            self.assertEqual(payload["run_key_name"], output.run_key_name)

    def test_write_node_success_state_uses_bounded_manifest_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager("Workflow", base_dir=Path(tmp_dir))
            node_state = NodeState(
                run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
                plan_schema_version=SCHEMA_VERSION,
                workflow_identity=".orchestrator/workflows/workflow.task.md",
                workflow_name="workflow",
                workflow_signature=_workflow_signature("workflow"),
                run_id=output.run_id,
                run_key_name=output.run_key_name,
                node_id="build.node",
                completed_at=datetime(2026, 6, 3, 12, 0).isoformat(),
                artifacts=[
                    ArtifactDescriptor(
                        kind="output",
                        relative_path="build.node-result.md",
                        sha256=_workflow_signature("result"),
                        size_bytes=6,
                    )
                ],
            )

            path = output.write_node_success_state(node_state)

            self.assertEqual(path.name, "build.node--811a9309e00c.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["node_id"], "build.node")

    def test_write_preflight_plan_and_static_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager("Workflow", base_dir=Path(tmp_dir))
            plan = _minimal_plan(output)

            static_path = output.write_preflight_static_file(
                "static-files/context.txt",
                b"context",
            )
            plan_path = output.write_preflight_plan(plan)
            manifest_path = output.write_preflight_manifest(
                {"status": "preflight_succeeded"}
            )
            diagnostics_path = output.write_preflight_diagnostics([])
            metadata_path = output.write_preflight_metadata({"run_id": output.run_id})
            render_path = output.write_preflight_render_plan([])
            bundle_path = output.write_preflight_execution_bundle({"nodes": []})
            summary_path = output.write_preflight_summary("# Preflight\n")

            self.assertEqual(static_path.read_text(encoding="utf-8"), "context")
            plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(
                plan_payload["workflow_signature"], plan.workflow_signature
            )
            self.assertEqual(plan_payload["plan_schema_version"], SCHEMA_VERSION)
            self.assertNotIn("schema_version", plan_payload)
            self.assertEqual(plan_payload["run_key_name"], output.run_key_name)
            self.assertEqual(manifest_path.name, "manifest.json")
            self.assertEqual(diagnostics_path.name, "diagnostics.json")
            self.assertEqual(metadata_path.name, "metadata.json")
            self.assertEqual(render_path.name, "render-plans.json")
            self.assertEqual(bundle_path.name, "execution-bundle.json")
            self.assertEqual(summary_path.read_text(encoding="utf-8"), "# Preflight\n")

    def test_run_manifest_signature_must_be_sha256_hex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager("Workflow", base_dir=Path(tmp_dir))

            with self.assertRaisesRegex(ValueError, "workflow_signature"):
                _running_manifest(
                    output,
                    workflow_signature="not-a-signature",
                )


if __name__ == "__main__":
    unittest.main()
