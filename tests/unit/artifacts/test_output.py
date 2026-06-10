from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from orchestrator_cli.artifacts import OutputManager
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
