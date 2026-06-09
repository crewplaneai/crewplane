import asyncio
import io
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

import orchestrator_cli.cli.templates as templates
import orchestrator_cli.cli.workflow_runner as workflow_runner
from orchestrator_cli.core.config import DEFAULT_INVOCATION_TIMEOUT_SECONDS, load_config
from orchestrator_cli.core.preflight import load_workflow_source_for_preflight
from orchestrator_cli.core.workflow_loader import load_tasks_with_sources
from orchestrator_cli.core.workflow_validation import validate_workflow_plan
from orchestrator_cli.core.yaml_loader import load_yaml_unique


def _redundant_direct_dependencies(workflow) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    dependency_map = {node.id: tuple(node.needs) for node in workflow.nodes}
    ancestors: dict[str, set[str]] = {node.id: set() for node in workflow.nodes}
    remaining_dependencies = {
        node_id: len(needs) for node_id, needs in dependency_map.items()
    }
    dependents: dict[str, list[str]] = {node.id: [] for node in workflow.nodes}

    for node_id, needs in dependency_map.items():
        for dependency_id in needs:
            dependents[dependency_id].append(node_id)

    ready = deque(
        node.id for node in workflow.nodes if remaining_dependencies[node.id] == 0
    )
    while ready:
        node_id = ready.popleft()
        for dependent_id in dependents[node_id]:
            ancestors[dependent_id].update(ancestors[node_id])
            ancestors[dependent_id].add(node_id)
            remaining_dependencies[dependent_id] -= 1
            if remaining_dependencies[dependent_id] == 0:
                ready.append(dependent_id)

    redundant_dependencies: list[tuple[str, str]] = []
    for node in workflow.nodes:
        for dependency_id in node.needs:
            other_upstream_nodes = {
                upstream_id
                for other_dependency_id in node.needs
                if other_dependency_id != dependency_id
                for upstream_id in (
                    other_dependency_id,
                    *ancestors[other_dependency_id],
                )
            }
            if dependency_id in other_upstream_nodes:
                redundant_dependencies.append((node.id, dependency_id))

    return redundant_dependencies


def _render_initialized_template_tree(rendered_root: Path) -> Path:
    orchestrator_dir = rendered_root / ".orchestrator"
    workflows_dir = orchestrator_dir / "workflows"
    workflow_library_dir = workflows_dir / "example-templates"
    input_dir = orchestrator_dir / "inputs"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_library_dir.mkdir(parents=True, exist_ok=True)
    input_dir.mkdir(parents=True, exist_ok=True)

    (orchestrator_dir / "config.yml").write_text(
        templates.render_template_content(
            templates.CONFIG_TEMPLATE.read_text(encoding="utf-8")
        ),
        encoding="utf-8",
    )
    (workflows_dir / "code-review-example.task.md").write_text(
        templates.render_template_content(
            templates.DEFAULT_WORKFLOW_TEMPLATE.read_text(encoding="utf-8")
        ),
        encoding="utf-8",
    )
    for relative_path in templates.discover_workflow_library_assets():
        target_path = workflow_library_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            templates.render_template_content(
                (templates.WORKFLOW_LIBRARY_TEMPLATE_DIR / relative_path).read_text(
                    encoding="utf-8"
                )
            ),
            encoding="utf-8",
        )
    for relative_path in templates.discover_input_assets():
        target_path = input_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            templates.render_template_content(
                (templates.INPUT_TEMPLATE_DIR / relative_path).read_text(
                    encoding="utf-8"
                )
            ),
            encoding="utf-8",
        )
    return orchestrator_dir


class ExampleTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template_dir = Path("src/orchestrator_cli/example_templates")

    def test_config_template_is_valid(self) -> None:
        config_path = self.template_dir / "config.yml"
        with tempfile.TemporaryDirectory() as tmp_dir:
            rendered_config = Path(tmp_dir) / "config.yml"
            rendered_config.write_text(
                templates.render_template_content(
                    config_path.read_text(encoding="utf-8")
                ),
                encoding="utf-8",
            )
            config = load_config(rendered_config)
        self.assertIn("claude", config.agents)
        self.assertIn("codex", config.agents)
        self.assertIn("gemini", config.agents)
        self.assertEqual(config.agents["claude"].default_model, "sonnet")
        self.assertIn("--bare", config.agents["claude"].extra_args)
        self.assertIn(
            "--dangerously-skip-permissions",
            config.agents["claude"].extra_args,
        )
        self.assertEqual(config.agents["codex"].default_model, "gpt-5.4")
        self.assertIn(
            "--dangerously-bypass-approvals-and-sandbox",
            config.agents["codex"].extra_args,
        )
        self.assertEqual(
            config.agents["gemini"].default_model,
            "auto",
        )
        self.assertIn("--approval-mode=yolo", config.agents["gemini"].extra_args)
        self.assertEqual(config.agents["gemini"].prompt_transport, "stdin")
        self.assertIsNone(config.agents["gemini"].prompt_transport_arg)
        for agent_config in config.agents.values():
            self.assertEqual(
                agent_config.invocation_timeout_seconds,
                DEFAULT_INVOCATION_TIMEOUT_SECONDS,
            )
        self.assertIsNone(config.agents["claude"].pricing.input)
        self.assertIsNone(config.agents["codex"].pricing.output)
        assert config.settings is not None
        self.assertEqual(config.settings.token_budget.warn_threshold_chars, 50000)
        self.assertIsNone(config.settings.token_budget.fail_threshold_chars)

    def test_built_in_provider_template_omits_generic_model_arg(self) -> None:
        rendered = templates.render_template_content(
            (self.template_dir / "config.yml").read_text(encoding="utf-8")
        )
        payload = load_yaml_unique(rendered)
        assert isinstance(payload, dict)
        agents = payload["agents"]
        assert isinstance(agents, dict)

        offenders = [
            agent_name
            for agent_name, agent_payload in agents.items()
            if isinstance(agent_payload, dict)
            and agent_payload.get("provider_kind") != "generic"
            and "model_arg" in agent_payload
        ]
        self.assertEqual(offenders, [])

    def test_workflow_markdown_template_is_valid(self) -> None:
        workflow_templates = sorted(self.template_dir.rglob("*.task.md"))
        self.assertGreaterEqual(len(workflow_templates), 1)

        with tempfile.TemporaryDirectory() as tmp_dir:
            rendered_root = Path(tmp_dir)
            for workflow_path in workflow_templates:
                rendered_workflow = rendered_root / workflow_path.relative_to(
                    self.template_dir
                )
                rendered_workflow.parent.mkdir(parents=True, exist_ok=True)
                rendered_workflow.write_text(
                    templates.render_template_content(
                        workflow_path.read_text(encoding="utf-8")
                    ),
                    encoding="utf-8",
                )
            for workflow_path in workflow_templates:
                rendered_workflow = rendered_root / workflow_path.relative_to(
                    self.template_dir
                )
                workflow = validate_workflow_plan(
                    load_tasks_with_sources(
                        rendered_workflow,
                        project_root=rendered_root,
                    ).workflow
                )
                self.assertGreaterEqual(len(workflow.nodes), 1)

    def test_workflow_templates_avoid_redundant_transitive_dependencies(self) -> None:
        workflow_templates = sorted(self.template_dir.rglob("*.task.md"))
        self.assertGreaterEqual(len(workflow_templates), 1)

        with tempfile.TemporaryDirectory() as tmp_dir:
            rendered_root = Path(tmp_dir)
            for workflow_path in workflow_templates:
                rendered_workflow = rendered_root / workflow_path.relative_to(
                    self.template_dir
                )
                rendered_workflow.parent.mkdir(parents=True, exist_ok=True)
                rendered_workflow.write_text(
                    templates.render_template_content(
                        workflow_path.read_text(encoding="utf-8")
                    ),
                    encoding="utf-8",
                )

            for workflow_path in workflow_templates:
                rendered_workflow = rendered_root / workflow_path.relative_to(
                    self.template_dir
                )
                workflow = validate_workflow_plan(
                    load_tasks_with_sources(
                        rendered_workflow,
                        project_root=rendered_root,
                    ).workflow
                )
                self.assertEqual(
                    _redundant_direct_dependencies(workflow),
                    [],
                    msg=f"{workflow_path} declares redundant direct dependencies",
                )

    def test_legacy_yaml_template_not_shipped(self) -> None:
        self.assertFalse((self.template_dir / "tasks.yaml").exists())

    def test_library_template_discovery_is_recursive_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            library_dir = Path(tmp_dir)
            (library_dir / "b").mkdir(parents=True)
            (library_dir / "z.task.md").write_text("x", encoding="utf-8")
            (library_dir / "b" / "a.task.md").write_text("x", encoding="utf-8")
            (library_dir / "ignore.md").write_text("x", encoding="utf-8")

            with patch.object(
                templates,
                "WORKFLOW_LIBRARY_TEMPLATE_DIR",
                library_dir,
            ):
                discovered = templates.discover_workflow_library_templates()

        self.assertEqual(
            discovered,
            [Path("b/a.task.md"), Path("z.task.md")],
        )

    def test_workflow_library_asset_discovery_includes_nested_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            library_dir = Path(tmp_dir)
            (library_dir / "composition").mkdir(parents=True)
            (library_dir / "root.task.md").write_text("x", encoding="utf-8")
            (library_dir / "composition" / "child.task.md").write_text(
                "x",
                encoding="utf-8",
            )

            with patch.object(
                templates,
                "WORKFLOW_LIBRARY_TEMPLATE_DIR",
                library_dir,
            ):
                discovered = templates.discover_workflow_library_assets()

        self.assertEqual(
            discovered,
            [Path("composition/child.task.md"), Path("root.task.md")],
        )

    def test_input_asset_discovery_is_recursive_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_dir = Path(tmp_dir)
            (input_dir / "nested").mkdir(parents=True)
            (input_dir / "review-findings.md").write_text("x", encoding="utf-8")
            (input_dir / "nested" / "coding-standards.md").write_text(
                "x",
                encoding="utf-8",
            )

            with patch.object(templates, "INPUT_TEMPLATE_DIR", input_dir):
                discovered = templates.discover_input_assets()

        self.assertEqual(
            discovered,
            [Path("nested/coding-standards.md"), Path("review-findings.md")],
        )

    def test_composition_example_templates_compose_after_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            rendered_root = Path(tmp_dir)
            for template_path in self.template_dir.rglob("*"):
                if not template_path.is_file():
                    continue
                rendered_path = rendered_root / template_path.relative_to(
                    self.template_dir
                )
                rendered_path.parent.mkdir(parents=True, exist_ok=True)
                rendered_path.write_text(
                    templates.render_template_content(
                        template_path.read_text(encoding="utf-8")
                    ),
                    encoding="utf-8",
                )

            workflow_path = (
                rendered_root
                / "example-templates"
                / "composition"
                / "review-fix-composed-example.task.md"
            )
            workflow = validate_workflow_plan(
                load_tasks_with_sources(
                    workflow_path, project_root=rendered_root
                ).workflow
            )

        self.assertEqual(
            [node.id for node in workflow.nodes],
            [
                "quality.review.findings",
                "fix.implement.execute",
                "fix.implement.summary",
                "handoff.standards",
                "handoff.final",
            ],
        )

    def test_initialized_workflow_templates_compile_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            rendered_root = Path(tmp_dir)
            orchestrator_dir = _render_initialized_template_tree(rendered_root)
            config = load_config(orchestrator_dir / "config.yml")
            assert config.settings is not None
            config.settings.integrations.invoker.implementation = "mock"
            config.settings.integrations.invoker.options = {
                "delay_seconds": 0,
                "observation_delay_seconds": 0,
                "output_mode": "lorem",
                "seed": 42,
            }

            for workflow_path in sorted(
                (orchestrator_dir / "workflows").rglob("*.task.md")
            ):
                source = load_workflow_source_for_preflight(
                    workflow_path,
                    project_root=rendered_root,
                )
                preview = workflow_runner.compile_workflow_preview(
                    config=config,
                    source=source,
                    console=Console(file=io.StringIO(), force_terminal=False),
                    no_live=True,
                    fingerprint_key_policy="read_only",
                    project_root=rendered_root,
                    orchestrator_dir=orchestrator_dir,
                    check_cli_availability=False,
                )
                self.assertFalse(
                    preview.has_errors(),
                    msg=f"{workflow_path} preflight diagnostics: {preview.diagnostics}",
                )

    def test_code_review_example_runs_with_mock_and_records_estimated_usage(
        self,
    ) -> None:
        config_path = self.template_dir / "config.yml"
        workflow_path = self.template_dir / "code-review-example.task.md"

        with tempfile.TemporaryDirectory() as tmp_dir:
            rendered_root = Path(tmp_dir)
            orchestrator_dir = rendered_root / ".orchestrator"
            workflows_dir = orchestrator_dir / "workflows"
            workflows_dir.mkdir(parents=True, exist_ok=True)

            rendered_config_path = orchestrator_dir / "config.yml"
            rendered_workflow_path = workflows_dir / "code-review-example.task.md"
            rendered_config_path.write_text(
                templates.render_template_content(
                    config_path.read_text(encoding="utf-8")
                ),
                encoding="utf-8",
            )
            rendered_workflow_path.write_text(
                templates.render_template_content(
                    workflow_path.read_text(encoding="utf-8")
                ),
                encoding="utf-8",
            )

            config = load_config(rendered_config_path)
            assert config.settings is not None
            config.settings.integrations.invoker.implementation = "mock"
            config.settings.integrations.invoker.options = {
                "delay_seconds": 0,
                "observation_delay_seconds": 0,
                "output_mode": "lorem",
                "seed": 42,
            }
            source = load_workflow_source_for_preflight(
                rendered_workflow_path,
                project_root=rendered_root,
            )
            validate_workflow_plan(source.workflow)

            original_cwd = Path.cwd()
            stream = io.StringIO()
            try:
                os.chdir(rendered_root)
                asyncio.run(
                    workflow_runner.execute_workflow_run(
                        config=config,
                        source=source,
                        force=False,
                        no_live=True,
                        console=Console(file=stream, force_terminal=False),
                    )
                )
            finally:
                os.chdir(original_cwd)

            execution_stage_root = next(
                (rendered_root / ".orchestrator" / "execution-stages").iterdir()
            )
            execution_results_root = next(
                (rendered_root / ".orchestrator" / "execution-results").iterdir()
            )
            summary_path = execution_stage_root / "logs" / "summary.md"
            event_log_path = execution_stage_root / "logs" / "events.ndjson"
            findings_path = execution_results_root / "review.context-findings.md"

            self.assertTrue(findings_path.exists())
            self.assertIn(
                "Synthetic finding for review.context",
                findings_path.read_text(encoding="utf-8"),
            )
            self.assertTrue(summary_path.exists())
            self.assertTrue(event_log_path.exists())

            event_log_text = event_log_path.read_text(encoding="utf-8")
            summary_text = summary_path.read_text(encoding="utf-8")
            self.assertIn('"provider_usage_status": "none"', event_log_text)
            self.assertIn('"visible_estimate_tokens":', event_log_text)
            self.assertIn('"output_extraction_status": "success"', event_log_text)
            self.assertIn("## Spend Observability", summary_text)
            self.assertIn("CLI invocations captured:", summary_text)
            self.assertIn("Visible-text estimate (lower-bound):", summary_text)
            self.assertIn("Run Summary", stream.getvalue())
            self.assertIn("Visible-text estimate (lower-bound):", stream.getvalue())
