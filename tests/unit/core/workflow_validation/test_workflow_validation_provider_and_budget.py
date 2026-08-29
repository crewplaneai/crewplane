import shutil
import stat
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from crewplane.adapters.invokers.cli import collect_cli_availability_errors
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.graph import topological_waves
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.core.workflow.validation import (
    collect_provider_validation_errors,
    collect_token_budget_validation_errors,
    validate_token_budget_settings,
    validate_workflow_plan,
)
from crewplane.version import SCHEMA_VERSION


class WorkflowValidationProviderAndBudgetTests(unittest.TestCase):
    def test_provider_validation_reports_unknown_provider(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="p")
                    ],
                    providers=[ProviderSpec(provider="missing-provider")],
                )
            ],
        )
        config = Config(
            version=SCHEMA_VERSION,
            agents={
                "known": AgentConfig(cli_cmd=["echo"], default_model="x"),
            },
        )

        errors = collect_provider_validation_errors(workflow, config)
        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown provider 'missing-provider'", errors[0])

    def test_cli_adapter_validation_reports_missing_cli(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="p")
                    ],
                    providers=[
                        ProviderSpec(provider="python-agent"),
                        ProviderSpec(provider="ghost-agent"),
                    ],
                )
            ],
        )
        config = Config(
            version=SCHEMA_VERSION,
            agents={
                "python-agent": AgentConfig(cli_cmd=["python3"], default_model="x"),
                "ghost-agent": AgentConfig(
                    cli_cmd=["definitely-not-real-executable-12345"],
                    default_model="x",
                ),
            },
        )

        errors = collect_cli_availability_errors(
            workflow,
            config,
            which_fn=lambda executable: executable if executable == "python3" else None,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("ghost-agent", errors[0])

    def test_cli_adapter_validation_reports_missing_env_wrapped_cli(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="p")
                    ],
                    providers=[ProviderSpec(provider="wrapped-agent")],
                )
            ],
        )
        missing_executable = "crewplane-provider-that-does-not-exist"
        config = Config(
            version=SCHEMA_VERSION,
            agents={
                "wrapped-agent": AgentConfig(
                    cli_cmd=["env", "CREWPLANE_REPRO=1", missing_executable],
                    default_model="x",
                ),
            },
        )

        probed_executables: list[str] = []

        def executable_lookup(executable: str) -> str | None:
            probed_executables.append(executable)
            return _platform_env_executable(executable)

        errors = collect_cli_availability_errors(
            workflow,
            config,
            which_fn=executable_lookup,
        )

        self.assertEqual(probed_executables, ["env", missing_executable])
        self.assertEqual(len(errors), 1)
        self.assertIn(f"CLI '{missing_executable}' not found in PATH", errors[0])
        self.assertIn("wrapped-agent", errors[0])

    def test_cli_adapter_validation_reports_missing_path_qualified_env_cli(
        self,
    ) -> None:
        missing_executable = "crewplane-provider-that-does-not-exist"

        errors = _collect_wrapped_cli_errors(
            ["/usr/bin/env", missing_executable],
            Path.cwd(),
            _platform_env_executable,
        )

        self.assertEqual(len(errors), 1)
        self.assertIn(f"CLI '{missing_executable}' not found in PATH", errors[0])

    def test_cli_adapter_validation_resolves_path_qualified_custom_env_from_project_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            _write_executable(project_root / "env")

            def launcher_lookup(executable: str) -> str | None:
                return "/usr/bin/env" if executable == "./env" else None

            errors = _collect_wrapped_cli_errors(
                ["./env", "serve"],
                project_root,
                launcher_lookup,
            )

        self.assertEqual(errors, [])

    def test_cli_adapter_validation_preserves_path_resolved_custom_env(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            _write_executable(project_root / "env")

            with patch.dict("os.environ", {"PATH": str(project_root)}):
                errors = _collect_wrapped_cli_errors(
                    ["env", "serve"],
                    project_root,
                    shutil.which,
                )

        self.assertEqual(errors, [])

    def test_cli_adapter_validation_uses_env_path_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            _write_executable(project_root / "configured-bin" / "custom-provider")

            errors = _collect_wrapped_cli_errors(
                ["env", "PATH=configured-bin", "custom-provider"],
                project_root,
                _platform_env_executable,
            )

        self.assertEqual(errors, [])

    def test_cli_adapter_validation_uses_env_reset_after_option_terminator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            _write_executable(project_root / "configured-bin" / "custom-provider")

            errors = _collect_wrapped_cli_errors(
                [
                    "env",
                    "--",
                    "-",
                    "PATH=configured-bin",
                    "custom-provider",
                ],
                project_root,
                _platform_env_executable,
            )

        self.assertEqual(errors, [])

    def test_cli_adapter_validation_treats_env_terminator_after_reset_as_command(
        self,
    ) -> None:
        errors = _collect_wrapped_cli_errors(
            ["env", "-", "--"],
            Path.cwd(),
            _platform_env_executable,
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("CLI '--' not found in PATH", errors[0])

    def test_cli_adapter_validation_resolves_inherited_relative_env_path_from_project(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            _write_executable(project_root / "inherited-bin" / "custom-provider")

            with patch.dict("os.environ", {"PATH": "inherited-bin"}):
                errors = _collect_wrapped_cli_errors(
                    ["env", "custom-provider"],
                    project_root,
                    _platform_env_executable,
                )

        self.assertEqual(errors, [])

    def test_cli_adapter_validation_uses_env_explicit_search_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            _write_executable(project_root / "explicit-bin" / "custom-provider")

            errors = _collect_wrapped_cli_errors(
                [
                    "env",
                    "-Pexplicit-bin",
                    "PATH=missing-bin",
                    "custom-provider",
                ],
                project_root,
                _platform_env_executable,
            )

        self.assertEqual(errors, [])

    def test_cli_adapter_validation_reports_missing_cli_after_env_chdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            (project_root / "work").mkdir()

            errors = _collect_wrapped_cli_errors(
                ["env", "-C", "work", "missing-provider"],
                project_root,
                _platform_env_executable,
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("CLI 'missing-provider' not found in PATH", errors[0])

    def test_cli_adapter_validation_uses_env_chdir_for_relative_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            _write_executable(project_root / "work" / "bin" / "custom-provider")

            errors = _collect_wrapped_cli_errors(
                ["env", "--chdir=work", "./bin/custom-provider"],
                project_root,
                _platform_env_executable,
            )

        self.assertEqual(errors, [])

    def test_cli_adapter_validation_uses_env_chdir_for_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            _write_executable(project_root / "work" / "bin" / "custom-provider")

            errors = _collect_wrapped_cli_errors(
                ["env", "-Cwork", "PATH=bin", "custom-provider"],
                project_root,
                _platform_env_executable,
            )

        self.assertEqual(errors, [])

    def test_cli_adapter_validation_rejects_provider_outside_env_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            (project_root / "configured-bin").mkdir()

            def executable_lookup(executable: str) -> str | None:
                if executable == "env":
                    return _platform_env_executable(executable)
                return f"/parent/{executable}"

            errors = _collect_wrapped_cli_errors(
                ["env", "PATH=configured-bin", "parent-provider"],
                project_root,
                executable_lookup,
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("CLI 'parent-provider' not found in PATH", errors[0])

    def test_cli_adapter_validation_rechecks_env_when_wrapped_with_custom_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            (project_root / "configured-bin").mkdir()

            errors = _collect_wrapped_cli_errors(
                ["env", "PATH=configured-bin", "env"],
                project_root,
                _platform_env_executable,
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("CLI 'env' not found in PATH", errors[0])

    def test_cli_adapter_validation_checks_relative_path_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            executable = project_root / "tools" / "provider"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            workflow = WorkflowPlan(
                name="Workflow",
                nodes=[
                    WorkflowNode(
                        id="node.a",
                        mode="parallel",
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="p")
                        ],
                        providers=[ProviderSpec(provider="local-agent")],
                    )
                ],
            )
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "local-agent": AgentConfig(
                        cli_cmd=["tools/provider"],
                        default_model="x",
                    ),
                },
            )

            errors = collect_cli_availability_errors(
                workflow,
                config,
                which_fn=_missing_executable,
                project_root=project_root,
            )

        self.assertEqual(errors, [])

    def test_cli_adapter_validation_rejects_non_executable_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            executable = project_root / "tools" / "provider"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode & ~stat.S_IXUSR)
            workflow = WorkflowPlan(
                name="Workflow",
                nodes=[
                    WorkflowNode(
                        id="node.a",
                        mode="parallel",
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="p")
                        ],
                        providers=[ProviderSpec(provider="local-agent")],
                    )
                ],
            )
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "local-agent": AgentConfig(
                        cli_cmd=["tools/provider"],
                        default_model="x",
                    ),
                },
            )

            errors = collect_cli_availability_errors(
                workflow,
                config,
                which_fn=_missing_executable,
                project_root=project_root,
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("local-agent", errors[0])
        self.assertIn("not found or not executable", errors[0])

    def test_provider_validation_ignores_cli_availability(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="p")
                    ],
                    providers=[ProviderSpec(provider="ghost-agent")],
                )
            ],
        )
        config = Config(
            version=SCHEMA_VERSION,
            agents={
                "ghost-agent": AgentConfig(
                    cli_cmd=["definitely-not-real-executable-12345"],
                    default_model="x",
                ),
            },
        )

        errors = collect_provider_validation_errors(workflow, config)

        self.assertEqual(errors, [])

    def test_provider_validation_reports_unknown_provider_only(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="p")
                    ],
                    providers=[ProviderSpec(provider="missing-provider")],
                )
            ],
        )
        config = Config(version=SCHEMA_VERSION, agents={})

        errors = collect_provider_validation_errors(workflow, config)

        self.assertEqual(len(errors), 1)
        self.assertIn("Unknown provider 'missing-provider'", errors[0])

    def test_input_node_rejects_token_budget(self) -> None:
        workflow = WorkflowPlan(
            name="Invalid input node",
            nodes=[
                WorkflowNode(
                    id="review-input",
                    mode="input",
                    source="{{file:.crewplane/inputs/review-findings.md}}",
                    token_budget={"warn_threshold_chars": 1000},
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "must not define token_budget"):
            validate_workflow_plan(workflow)

    def test_token_budget_validation_reports_invalid_merged_thresholds(self) -> None:
        workflow = WorkflowPlan(
            name="Workflow",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                    ],
                    token_budget={"fail_threshold_chars": 900},
                    providers=[
                        ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                    ],
                )
            ],
        )
        config = Config(
            version=SCHEMA_VERSION,
            agents={
                "alpha": AgentConfig(cli_cmd=["echo"], default_model="x"),
            },
            settings=Settings(
                token_budget={
                    "warn_threshold_chars": 1000,
                }
            ),
        )

        errors = collect_token_budget_validation_errors(workflow, config)
        self.assertEqual(len(errors), 1)
        self.assertIn("node.a", errors[0])
        with self.assertRaisesRegex(ValueError, "node.a"):
            validate_token_budget_settings(workflow, config)

    def test_topological_waves_preserve_frontmatter_order(self) -> None:
        workflow = WorkflowPlan(
            name="Ordered DAG",
            nodes=[
                WorkflowNode(
                    id="node.z",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="z")
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                    ],
                    providers=[ProviderSpec(provider="gpt4")],
                ),
                WorkflowNode(
                    id="node.summary",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="sum")
                    ],
                    needs=["node.z", "node.a"],
                    providers=[
                        ProviderSpec(provider="gpt4", role=ProviderRole.EXECUTOR)
                    ],
                ),
            ],
        )
        waves = topological_waves(workflow)
        self.assertEqual(waves[0], ["node.z", "node.a"])


def _missing_executable(executable: str) -> str | None:  # noqa: ARG001
    return None


def _platform_env_executable(executable: str) -> str | None:
    if executable not in {"env", "/usr/bin/env"}:
        return None
    return "/usr/bin/env"


def _write_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _collect_wrapped_cli_errors(
    cli_cmd: list[str],
    project_root: Path,
    which_fn: Callable[[str], str | None],
) -> list[str]:
    workflow = WorkflowPlan(
        name="Workflow",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="p")
                ],
                providers=[ProviderSpec(provider="wrapped-agent")],
            )
        ],
    )
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            "wrapped-agent": AgentConfig(cli_cmd=cli_cmd, default_model="x"),
        },
    )
    return collect_cli_availability_errors(
        workflow,
        config,
        which_fn=which_fn,
        project_root=project_root,
    )
