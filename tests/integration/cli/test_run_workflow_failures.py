from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import typer

import crewplane.cli.app as cli
from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    InvocationContext,
    JsonObject,
    LogPresentationDescriptor,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.runtime.agent.failures import (
    InvocationFailureError,
    InvocationFailureSummary,
)
from crewplane.version import SCHEMA_VERSION
from tests.integration.cli.cli_workflow_helpers import ConsoleFactory


def _provider_failure(message: str) -> InvocationFailureError:
    summary = InvocationFailureSummary(
        kind="quota_or_rate_limit",
        phase="provider_transport",
        source="stderr_text",
        message=message,
        advice="The provider reported quota or capacity pressure.",
        condensed=False,
    )
    return InvocationFailureError("provider invocation failed", summary, None)


class CapacityFailureInvoker:
    def log_presentation_for(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by invoker protocol.
    ) -> LogPresentationDescriptor | None:
        return None

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by invoker protocol.
        model: str | None,
        prompt: str,  # noqa: ARG002 - Required by invoker protocol.
        output_file: Path,
        cwd: Path,  # noqa: ARG002 - Required by invoker protocol.
        log_file: Path | None = None,  # noqa: ARG002 - Required by invoker protocol.
        invocation_context: InvocationContext | None = None,  # noqa: ARG002 - Required by invoker protocol.
    ) -> None:
        if model in {"model-b", "model-c"}:
            raise _provider_failure("Selected model is at capacity. [/foo]")
        output_file.write_text("success", encoding="utf-8")


class CapacityFailureInvokerAdapter:
    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        if options:
            raise ValueError(f"Unsupported options: {sorted(options)}")
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options={},
            option_scopes={},
        )

    def create_invoker(
        self,
        config: Config,  # noqa: ARG002 - Required by adapter protocol.
        options: JsonObject | None = None,  # noqa: ARG002 - Required by adapter protocol.
    ) -> CapacityFailureInvoker:
        return CapacityFailureInvoker()


def _write_failure_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f'version: "{SCHEMA_VERSION}"',
                "",
                "agents:",
                "  alpha:",
                '    cli_cmd: ["echo"]',
                '    default_model: "model-a"',
                "  beta:",
                '    cli_cmd: ["echo"]',
                '    default_model: "model-b"',
                "  gamma:",
                '    cli_cmd: ["echo"]',
                '    default_model: "model-c"',
                "settings:",
                "  integrations:",
                "    invoker:",
                f'      implementation: "{__name__}:CapacityFailureInvokerAdapter"',
                "      options: {}",
                "    ui:",
                '      implementation: "none"',
                "      options: {}",
            ]
        ),
        encoding="utf-8",
    )


def _write_failure_workflow(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Capacity Failure",
                "description: deterministic provider failure",
                "nodes:",
                "  - id: compare.models",
                "    mode: parallel",
                "    providers: [alpha, beta]",
                "    failure_threshold: 0",
                "  - id: summarize.results",
                "    mode: parallel",
                "    needs: [compare.models]",
                "    providers: [alpha]",
                "---",
                "",
                "## compare.models",
                "",
                "Compare the models.",
                "",
                "## summarize.results",
                "",
                "Summarize the comparison.",
            ]
        ),
        encoding="utf-8",
    )


def _write_reviewer_failure_workflow(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Reviewer Capacity Failure",
                "description: deterministic reviewer provider failure",
                "nodes:",
                "  - id: review.candidate",
                "    mode: sequential",
                "    providers:",
                "      - provider: alpha",
                "        role: executor",
                "      - provider: beta",
                "        role: reviewer",
                "      - provider: gamma",
                "        role: reviewer",
                "  - id: summarize.review",
                "    mode: parallel",
                "    needs: [review.candidate]",
                "    providers: [alpha]",
                "---",
                "",
                "## review.candidate",
                "",
                "<!-- crewplane:executor -->",
                "Draft a candidate.",
                "<!-- /crewplane:executor -->",
                "",
                "<!-- crewplane:reviewer -->",
                "Review the candidate.",
                "<!-- /crewplane:reviewer -->",
                "",
                "## summarize.review",
                "",
                "Summarize the review.",
            ]
        ),
        encoding="utf-8",
    )


def _write_findings_failure_workflow(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Findings Extraction Failure",
                "description: deterministic findings extraction failure",
                "nodes:",
                "  - id: extract.findings",
                "    mode: parallel",
                "    findings: true",
                "    providers: [alpha]",
                "  - id: summarize.findings",
                "    mode: parallel",
                "    needs: [extract.findings]",
                "    providers: [alpha]",
                "---",
                "",
                "## extract.findings",
                "",
                "Produce findings.",
                "",
                "## summarize.findings",
                "",
                "Summarize findings.",
            ]
        ),
        encoding="utf-8",
    )


class CliRunWorkflowFailureTests(unittest.TestCase):
    def test_expected_workflow_failure_is_concise_and_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_dir = tmp_path / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            workflow_path = workflows_dir / "failure.task.md"
            _write_failure_config(config_path)
            _write_failure_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_cwd = Path.cwd()
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            os.chdir(tmp_path)
            try:
                with self.assertRaises(typer.Exit) as raised:
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                        force=False,
                        no_live=True,
                    )
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            self.assertEqual(raised.exception.exit_code, 1)
            output_text = stream.getvalue()
            self.assertIn("Workflow 'Capacity Failure' failed:", output_text)
            self.assertIn("- failed: compare.models", output_text)
            self.assertIn("Selected model is at capacity. [/foo]", output_text)
            self.assertIn(
                "- blocked: summarize.results "
                "(unsatisfied dependencies: compare.models)",
                output_text,
            )
            self.assertNotIn("Traceback", output_text)

            run_dirs = [
                path
                for path in (state_dir / "execution-stages").iterdir()
                if path.is_dir()
            ]
            self.assertEqual(len(run_dirs), 1)
            manifest = json.loads(
                (run_dirs[0] / "manifests" / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertIsNotNone(manifest["completed_at"])
            self.assertIn("compare.models", manifest["failure_message"])
            self.assertIn("summarize.results", manifest["failure_message"])

            summary_text = (run_dirs[0] / "logs" / "summary.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("- Status: failed", summary_text)
            self.assertIn("compare.models", summary_text)
            self.assertIn("Selected model is at capacity. [/foo]", summary_text)
            self.assertIn("summarize.results", summary_text)
            self.assertIn(f"Stages: {run_dirs[0]}", output_text)
            self.assertIn(
                f"Results: {state_dir / 'execution-results' / run_dirs[0].name}",
                output_text,
            )
            self.assertIn(f"Logs: {run_dirs[0] / 'logs'}", output_text)

    def test_reviewer_workflow_failure_is_concise_and_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_dir = tmp_path / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            workflow_path = workflows_dir / "reviewer-failure.task.md"
            _write_failure_config(config_path)
            _write_reviewer_failure_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_cwd = Path.cwd()
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            os.chdir(tmp_path)
            try:
                with self.assertRaises(typer.Exit) as raised:
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                        force=False,
                        no_live=True,
                    )
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            self.assertEqual(raised.exception.exit_code, 1)
            output_text = stream.getvalue()
            self.assertIn("Workflow 'Reviewer Capacity Failure' failed:", output_text)
            self.assertIn("- failed: review.candidate", output_text)
            self.assertIn(
                "Reviewer invocation failed for node 'review.candidate'",
                output_text,
            )
            self.assertIn(
                "- blocked: summarize.review "
                "(unsatisfied dependencies: review.candidate)",
                output_text,
            )
            self.assertNotIn("Traceback", output_text)

            run_dirs = [
                path
                for path in (state_dir / "execution-stages").iterdir()
                if path.is_dir()
            ]
            self.assertEqual(len(run_dirs), 1)
            manifest = json.loads(
                (run_dirs[0] / "manifests" / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("review.candidate", manifest["failure_message"])
            self.assertIn("summarize.review", manifest["failure_message"])

    def test_findings_extraction_failure_is_concise_and_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_dir = tmp_path / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            workflow_path = workflows_dir / "findings-failure.task.md"
            _write_failure_config(config_path)
            _write_findings_failure_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_cwd = Path.cwd()
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            os.chdir(tmp_path)
            try:
                with self.assertRaises(typer.Exit) as raised:
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                        force=False,
                        no_live=True,
                    )
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            self.assertEqual(raised.exception.exit_code, 1)
            output_text = stream.getvalue()
            self.assertIn("Workflow 'Findings Extraction Failure' failed:", output_text)
            self.assertIn("- failed: extract.findings", output_text)
            self.assertIn("Expected exactly one findings block", output_text)
            self.assertIn(
                "- blocked: summarize.findings "
                "(unsatisfied dependencies: extract.findings)",
                output_text,
            )
            self.assertNotIn("Traceback", output_text)

            run_dirs = [
                path
                for path in (state_dir / "execution-stages").iterdir()
                if path.is_dir()
            ]
            self.assertEqual(len(run_dirs), 1)
            manifest = json.loads(
                (run_dirs[0] / "manifests" / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("extract.findings", manifest["failure_message"])
            self.assertIn("summarize.findings", manifest["failure_message"])
