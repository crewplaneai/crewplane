import asyncio
import io
import unittest
from contextlib import chdir
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

import pytest
from rich.console import Console

import crewplane.cli.app as cli
import crewplane.cli.templates as templates
import crewplane.cli.workflow_runner as workflow_runner
from crewplane.adapters.invokers.cli_invoker.providers.codex import decode_codex_usage
from crewplane.architecture.contracts import CommandResult
from crewplane.cli.onboarding.rendering import (
    manual_config_snippet,
    rendered_default_config,
)
from crewplane.core.config import (
    DEFAULT_INVOCATION_IDLE_TIMEOUT_SECONDS,
    DEFAULT_INVOCATION_TIMEOUT_SECONDS,
    AgentConfig,
    load_config,
)
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.validation import validate_workflow_plan
from crewplane.core.yaml_loader import load_yaml_unique
from crewplane.runtime.agent.usage_costs import derive_configured_cost
from crewplane.version import SCHEMA_VERSION
from tests.helpers.isolated_git import run_git
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.working_directory import temporary_project_cwd

LEGACY_PROMPT_FIELDS = {"prompt_arg", "quota_parser", "stdin_prompt_arg", "use_stdin"}


def test_generated_config_does_not_reference_legacy_prompt_fields() -> None:
    config = load_yaml_unique(rendered_default_config())
    assert config["agents"]
    for agent in config["agents"].values():
        assert not LEGACY_PROMPT_FIELDS.intersection(agent)


class ExampleTemplateTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def temporary_directory_root(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def setUp(self) -> None:
        self.template_dir = Path("src/crewplane/example_templates")

    def test_config_template_is_valid(self) -> None:
        config_path = self.template_dir / "config.yml"
        tmp_dir = mkdtemp(dir=self.tmp_path)
        rendered_config = Path(tmp_dir) / "config.yml"
        rendered_config.write_text(
            templates.render_template_content(config_path.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        config = load_config(rendered_config)
        self.assertEqual(list(config.agents), ["mock"])
        self.assertEqual(
            config.agents["mock"].cli_cmd,
            ["__crewplane_mock_invoker_never_executes__"],
        )
        self.assertEqual(config.agents["mock"].default_model, "mock")
        self.assertEqual(config.agents["mock"].provider_kind, "generic")
        self.assertEqual(config.agents["mock"].prompt_transport, "stdin")
        self.assertIsNone(config.agents["mock"].prompt_transport_arg)
        self.assertIsNone(DEFAULT_INVOCATION_TIMEOUT_SECONDS)
        for agent_config in config.agents.values():
            self.assertIsNone(agent_config.invocation_timeout_seconds)
            self.assertEqual(
                agent_config.invocation_idle_timeout_seconds,
                DEFAULT_INVOCATION_IDLE_TIMEOUT_SECONDS,
            )
        self.assertEqual(config.agents["mock"].extra_args, [])
        assert config.settings is not None
        self.assertEqual(config.settings.integrations.invoker.implementation, "mock")
        self.assertEqual(
            config.settings.integrations.invoker.options,
            {
                "output_mode": "lorem",
                "seed": 42,
                "delay_seconds": 0.25,
                "observation_delay_seconds": 5,
            },
        )
        self.assertEqual(config.settings.token_budget.warn_threshold_chars, 50000)
        self.assertIsNone(config.settings.token_budget.fail_threshold_chars)

    def test_codex_example_pricing_uses_reported_input_tokens(self) -> None:
        snippet = manual_config_snippet(rendered_default_config(), ("codex",))
        snippet = snippet.replace("    # pricing:", "    pricing:").replace(
            "    #   ", "      "
        )
        payload = load_yaml_unique(snippet)
        assert isinstance(payload, dict)
        config = AgentConfig.model_validate(payload["codex"])
        usage = decode_codex_usage(
            CommandResult(
                returncode=0,
                stdout_text=(
                    '{"type":"turn.completed","usage":{"input_tokens":100000,'
                    '"cached_input_tokens":20000,"output_tokens":1000}}'
                ),
                stderr_text="",
            )
        )
        assert usage.tokens is not None

        cost, confidence = derive_configured_cost(
            config,
            usage.tokens,
            visible_input_tokens=100,
            visible_output_tokens=1000,
            visible_estimate_tokens=1100,
        )

        self.assertAlmostEqual(cost, 0.87)
        self.assertEqual(confidence, "full")

    def test_default_workflow_is_single_mock_provider_review(self) -> None:
        workflow_path = self.template_dir / "single-agent-review.task.md"
        rendered = templates.render_template_content(
            workflow_path.read_text(encoding="utf-8")
        )
        tmp_dir = mkdtemp(dir=self.tmp_path)
        rendered_root = Path(tmp_dir)
        rendered_workflow = rendered_root / "single-agent-review.task.md"
        rendered_workflow.write_text(rendered, encoding="utf-8")
        workflow = validate_workflow_plan(
            load_tasks_with_sources(
                rendered_workflow,
                project_root=rendered_root,
            ).workflow
        )

        self.assertEqual([node.id for node in workflow.nodes], ["review.project"])
        self.assertEqual(
            [provider.provider for provider in workflow.nodes[0].providers],
            ["mock"],
        )

    def test_library_template_discovery_is_recursive_and_sorted(self) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
        tmp_dir = mkdtemp(dir=self.tmp_path)
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

    def test_mock_workflow_records_estimated_usage(
        self,
    ) -> None:
        config_path = (self.template_dir / "config.yml").resolve()

        with temporary_project_cwd(self.tmp_path) as rendered_root:
            state_dir = rendered_root / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True, exist_ok=True)

            rendered_config_path = state_dir / "config.yml"
            rendered_workflow_path = workflows_dir / "code-review-example.task.md"
            rendered_config_path.write_text(
                templates.render_template_content(
                    config_path.read_text(encoding="utf-8")
                ),
                encoding="utf-8",
            )
            rendered_workflow_path.write_text(
                f"""---
schema_version: "{SCHEMA_VERSION}"
name: Usage capture
nodes:
  - id: review.context
    mode: parallel
    providers: [claude, codex, gemini]
    findings: true
  - id: review.iterate
    mode: sequential
    needs: [review.context]
    depth: 2
    audit_rounds: 2
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
      - provider: gemini
        role: reviewer
  - id: review.summary
    mode: sequential
    needs: [review.iterate]
    providers: [claude]
---
## review.context
Return findings about this test input.
## review.iterate
Review {{{{review.context.findings}}}}.
## review.summary
Summarize {{{{review.iterate.output}}}}.
""",
                encoding="utf-8",
            )

            config = load_config(rendered_config_path)
            assert config.settings is not None
            config.settings.integrations.invoker.options = {
                "delay_seconds": 0,
                "observation_delay_seconds": 0,
                "output_mode": "lorem",
                "seed": 42,
            }
            mock_agent = config.agents["mock"]
            for agent_name in ("claude", "codex", "gemini"):
                config.agents[agent_name] = mock_agent.model_copy(deep=True)
            source = load_workflow_source_for_preflight(
                rendered_workflow_path,
                project_root=rendered_root,
            )
            validate_workflow_plan(source.workflow)

            stream = io.StringIO()
            asyncio.run(
                workflow_runner.execute_workflow_run(
                    config=config,
                    source=source,
                    force=False,
                    no_live=True,
                    console=Console(file=stream, force_terminal=False),
                )
            )

            execution_stage_root = next(
                (rendered_root / ".crewplane" / "execution-stages").iterdir()
            )
            execution_results_root = next(
                (rendered_root / ".crewplane" / "execution-results").iterdir()
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


@pytest.mark.parametrize(
    "provider",
    ["claude", "codex", "gemini", "copilot", "kilo", "pi", "deepseek", "opencode"],
)
def test_onboarding_provider_config_is_valid_without_generic_model_arg(
    provider: str,
) -> None:
    payload = load_yaml_unique(
        manual_config_snippet(rendered_default_config(), (provider,))
    )
    assert set(payload) == {provider, "settings"}
    assert not LEGACY_PROMPT_FIELDS.intersection(payload[provider])
    config = AgentConfig.model_validate(payload[provider])
    assert config.cli_cmd
    if config.provider_kind != "generic":
        assert "model_arg" not in payload[provider]


TEMPLATE_ROOT = Path(__file__).resolve().parents[3] / "src/crewplane/example_templates"
WORKFLOW_PATHS = tuple(
    sorted(path.relative_to(TEMPLATE_ROOT) for path in TEMPLATE_ROOT.rglob("*.task.md"))
)


def test_legacy_yaml_template_not_shipped() -> None:
    assert not (TEMPLATE_ROOT / "tasks.yaml").exists()


@pytest.fixture(scope="module")
def initialized_templates(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("initialized-templates")
    with chdir(root):
        cli.init()
        change_request = root / "docs/crewplane-change-request.md"
        change_request.parent.mkdir()
        change_request.write_text(
            "Implement the requested feature.\n", encoding="utf-8"
        )
        run_git(root, "init")
        run_git(root, "add", ".")
        run_git(root, "commit", "-m", "initial")
    return root


@requires_workspace_support
@pytest.mark.parametrize("relative_path", WORKFLOW_PATHS, ids=str)
def test_initialized_workflow_compiles(
    initialized_templates: Path, relative_path: Path
) -> None:
    root = initialized_templates
    state_dir = root / ".crewplane"
    config = load_config(state_dir / "config.yml")
    assert config.settings is not None
    config.settings.workspace.enabled = True
    config.settings.workspace.cache_root = (root.parent / "workspace-cache").as_posix()
    source = load_workflow_source_for_preflight(
        state_dir / "workflows" / relative_path, project_root=root
    )
    workflow = validate_workflow_plan(source.workflow)
    assert workflow.nodes
    for node in workflow.nodes:
        for provider in node.providers:
            config.agents[provider.provider] = config.agents["mock"].model_copy(
                deep=True
            )
    preview = workflow_runner.compile_workflow_preview(
        config=config,
        source=source,
        console=Console(file=io.StringIO(), force_terminal=False),
        no_live=True,
        fingerprint_key_policy="read_only",
        project_root=root,
        state_dir=state_dir,
        check_cli_availability=False,
    )
    assert not preview.has_errors(), preview.diagnostics
