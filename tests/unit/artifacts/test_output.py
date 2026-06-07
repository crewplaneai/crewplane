from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    ExecutionPolicy,
    PreflightExecutionNode,
    PreflightExecutionPlan,
)


def _workflow_signature(label: str) -> str:
    import hashlib

    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _minimal_plan(output: OutputManager) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        run_id=output.run_id,
        run_key_name=output.stages_dir.name,
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
        runtime_config_snapshot={},
        effective_runtime_config_signature=_workflow_signature("runtime"),
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

    def test_successful_workflow_signature_manifest_dedupes_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            signature = _workflow_signature("abc123")
            first = OutputManager("Workflow", base_dir=base_dir)
            first.write_manifest(signature, {"status": "succeeded"})

            second = OutputManager("Workflow", base_dir=base_dir)

            self.assertTrue(second.workflow_signature_exists("Workflow", signature))

    def test_failed_or_corrupt_manifest_does_not_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            failed_signature = _workflow_signature("failed")
            corrupt_signature = _workflow_signature("corrupt")
            first = OutputManager("Workflow", base_dir=base_dir)
            first.write_manifest(failed_signature, {"status": "failed"})
            corrupt_dir = first.stages_dir / "manifests"
            corrupt_dir.mkdir(parents=True, exist_ok=True)
            (corrupt_dir / f"{corrupt_signature}.json").write_text(
                "{",
                encoding="utf-8",
            )

            second = OutputManager("Workflow", base_dir=base_dir)

            self.assertFalse(
                second.workflow_signature_exists("Workflow", failed_signature)
            )
            self.assertFalse(
                second.workflow_signature_exists("Workflow", corrupt_signature)
            )

    def test_newer_failed_or_corrupt_manifest_does_not_mask_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            failed_signature = _workflow_signature("success-then-failed")
            corrupt_signature = _workflow_signature("success-then-corrupt")
            first = OutputManager("Workflow", base_dir=base_dir)
            first.write_manifest(failed_signature, {"status": "succeeded"})
            first.write_manifest(corrupt_signature, {"status": "succeeded"})
            second = OutputManager("Workflow", base_dir=base_dir)
            second.write_manifest(failed_signature, {"status": "failed"})
            third = OutputManager("Workflow", base_dir=base_dir)
            corrupt_dir = third.stages_dir / "manifests"
            corrupt_dir.mkdir(parents=True, exist_ok=True)
            (corrupt_dir / f"{corrupt_signature}.json").write_text(
                "{",
                encoding="utf-8",
            )

            lookup = OutputManager("Workflow", base_dir=base_dir)

            self.assertTrue(
                lookup.workflow_signature_exists("Workflow", failed_signature)
            )
            self.assertTrue(
                lookup.workflow_signature_exists("Workflow", corrupt_signature)
            )

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
            self.assertEqual(plan_payload["plan_schema_version"], "1.0")
            self.assertNotIn("schema_version", plan_payload)
            self.assertEqual(plan_payload["run_key_name"], output.stages_dir.name)
            self.assertEqual(manifest_path.name, "manifest.json")
            self.assertEqual(diagnostics_path.name, "diagnostics.json")
            self.assertEqual(metadata_path.name, "metadata.json")
            self.assertEqual(render_path.name, "render-plans.json")
            self.assertEqual(bundle_path.name, "execution-bundle.json")
            self.assertEqual(summary_path.read_text(encoding="utf-8"), "# Preflight\n")

    def test_manifest_signature_must_be_sha256_hex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager("Workflow", base_dir=Path(tmp_dir))

            with self.assertRaisesRegex(ValueError, "workflow_signature"):
                output.write_manifest("not-a-signature", {"status": "succeeded"})


if __name__ == "__main__":
    unittest.main()
