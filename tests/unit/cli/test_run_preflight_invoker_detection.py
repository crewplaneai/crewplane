from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.architecture.contracts import CanonicalIntegrationConfig
from crewplane.architecture.errors import AdapterContractError
from crewplane.cli.run.preflight import (
    BUILTIN_CLI_INVOKER_IDENTITY,
    compile_workflow_preview,
    uses_cli_invoker,
    uses_mock_invoker,
)
from crewplane.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    PreflightWorkflowSource,
)
from crewplane.core.preflight.diagnostics import (
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION

TEST_MODULE = "tests.unit.cli.test_run_preflight_invoker_detection"


class NoAvailabilityInvokerAdapter:
    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: dict[str, object] | None = None,
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
        config: Config,
        options: dict[str, object] | None = None,
    ) -> object:
        del config, options
        raise AssertionError("preflight diagnostics must not create an invoker")


class SingleConstructionInvokerAdapter(NoAvailabilityInvokerAdapter):
    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1
        if type(self).instances > 1:
            raise AssertionError("preflight constructed the adapter twice")


class AvailabilityInvokerAdapter(NoAvailabilityInvokerAdapter):
    calls = 0

    def collect_availability_errors(
        self,
        workflow: WorkflowPlan,
        config: Config,
        project_root: Path,
        executable_lookup: Callable[[str], str | None] | None = None,
    ) -> tuple[str, ...]:
        del workflow, config, project_root, executable_lookup
        type(self).calls += 1
        return ("custom invoker is unavailable",)


class CliWrapperAdapter(CliInvokerAdapter):
    pass


class IdentitySpoofingCliAdapter(CliInvokerAdapter):
    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: dict[str, object] | None = None,
    ) -> CanonicalIntegrationConfig:
        canonical = super().canonicalize_options(
            implementation,
            resolved_identity,
            options,
        )
        return canonical.model_copy(
            update={"resolved_identity": BUILTIN_CLI_INVOKER_IDENTITY}
        )


def _workflow(reasoning: str | None = None) -> WorkflowPlan:
    return WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha", reasoning=reasoning)],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="build")
                ],
            )
        ],
    )


def _config_for(
    implementation: str,
    explicit_model_arg: bool = False,
) -> Config:
    agent = AgentConfig(
        cli_cmd=["missing-provider"],
        provider_kind="codex",
    )
    if explicit_model_arg:
        agent = AgentConfig(
            cli_cmd=["missing-provider"],
            provider_kind="codex",
            model_arg="--custom-model",
        )
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": agent},
        settings=Settings(
            integrations=IntegrationsConfig(
                invoker=IntegrationSpec(implementation=implementation),
                ui=IntegrationSpec(implementation="none"),
                artifacts=IntegrationSpec(implementation="filesystem"),
            )
        ),
    )


def _compile_with_availability(
    tmp_path: Path,
    implementation: str,
    executable_lookup: Callable[[str], str | None],
    reasoning: str | None = None,
    check_cli_availability: bool = True,
    explicit_model_arg: bool = False,
) -> PreflightCompilationPreview:
    workflow = _workflow(reasoning)
    return compile_workflow_preview(
        config=_config_for(implementation, explicit_model_arg),
        source=PreflightWorkflowSource.from_workflow(workflow),
        console=Console(file=None),
        no_live=True,
        fingerprint_key_policy="read_only",
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        check_cli_availability=check_cli_availability,
        which_fn=executable_lookup,
    )


@pytest.mark.parametrize(
    ("implementation", "expected_cli", "expected_mock"),
    [
        ("cli", True, False),
        ("mock", False, True),
        ("crewplane.adapters.invokers.cli:CliInvokerAdapter", True, False),
        ("crewplane.adapters.invokers.cli.CliInvokerAdapter", True, False),
        ("crewplane.adapters.invokers.mock:MockInvokerAdapter", False, True),
        ("crewplane.adapters.invokers.mock.MockInvokerAdapter", False, True),
        ("unknown", False, False),
        ("package.invokers:CustomInvokerAdapter", False, False),
    ],
)
def test_invoker_detection_preserves_alias_and_object_path_matching(
    implementation: str,
    expected_cli: bool,
    expected_mock: bool,
) -> None:
    config = Config(
        version=SCHEMA_VERSION,
        agents={},
        settings=Settings(
            integrations=IntegrationsConfig(
                invoker=IntegrationSpec(implementation=implementation),
            )
        ),
    )

    assert uses_cli_invoker(config) is expected_cli
    assert uses_mock_invoker(config) is expected_mock


def test_invoker_detection_preserves_default_cli_implementation() -> None:
    config = Config(version=SCHEMA_VERSION, agents={})

    assert uses_cli_invoker(config)
    assert not uses_mock_invoker(config)


def test_mock_invoker_performs_no_availability_probe(tmp_path: Path) -> None:
    def unexpected_probe(command: str) -> str | None:
        raise AssertionError(f"mock invoker probed {command}")

    preview = _compile_with_availability(
        tmp_path,
        "mock",
        unexpected_probe,
    )

    assert not preview.diagnostics


def test_builtin_cli_reports_ignored_model_arg_warning(tmp_path: Path) -> None:
    preview = _compile_with_availability(
        tmp_path,
        "cli",
        lambda command: f"/bin/{command}",
        explicit_model_arg=True,
    )

    assert len(preview.diagnostics) == 1
    diagnostic = preview.diagnostics[0]
    assert diagnostic.code is PreflightDiagnosticCode.PROVIDER_CONFIG
    assert diagnostic.phase is PreflightDiagnosticPhase.PROVIDER
    assert diagnostic.severity == "warning"
    assert "Agent 'alpha': remove model_arg" in diagnostic.message


@pytest.mark.parametrize(
    "implementation",
    [
        "mock",
        f"{TEST_MODULE}:NoAvailabilityInvokerAdapter",
        f"{TEST_MODULE}:CliWrapperAdapter",
    ],
)
def test_non_builtin_invokers_do_not_report_cli_model_arg_warning(
    tmp_path: Path,
    implementation: str,
) -> None:
    preview = _compile_with_availability(
        tmp_path,
        implementation,
        lambda command: f"/bin/{command}",
        explicit_model_arg=True,
    )

    assert not preview.diagnostics


def test_custom_invoker_without_hook_performs_no_availability_probe(
    tmp_path: Path,
) -> None:
    def unexpected_probe(command: str) -> str | None:
        raise AssertionError(f"custom invoker probed {command}")

    preview = _compile_with_availability(
        tmp_path,
        f"{TEST_MODULE}:NoAvailabilityInvokerAdapter",
        unexpected_probe,
    )

    assert not preview.diagnostics


def test_custom_invoker_availability_hook_is_not_called(tmp_path: Path) -> None:
    AvailabilityInvokerAdapter.calls = 0

    def unexpected_probe(command: str) -> str | None:
        raise AssertionError(f"custom invoker probed {command}")

    preview = _compile_with_availability(
        tmp_path,
        f"{TEST_MODULE}:AvailabilityInvokerAdapter",
        unexpected_probe,
    )

    assert AvailabilityInvokerAdapter.calls == 0
    assert not preview.diagnostics


def test_cli_wrapper_is_not_probed_but_remains_reasoning_ineligible(
    tmp_path: Path,
) -> None:
    probed_commands: list[str] = []

    def available(command: str) -> str | None:
        probed_commands.append(command)
        return f"/bin/{command}"

    preview = _compile_with_availability(
        tmp_path,
        f"{TEST_MODULE}:CliWrapperAdapter",
        available,
        reasoning="high",
    )

    assert probed_commands == []
    assert [diagnostic.message for diagnostic in preview.diagnostics] == [
        "workflow 'demo' -> node 'build' -> provider 'alpha': first-class "
        "reasoning requires the built-in CLI invoker."
    ]


def test_adapter_cannot_spoof_builtin_identity_for_reasoning(
    tmp_path: Path,
) -> None:
    preview = _compile_with_availability(
        tmp_path,
        f"{TEST_MODULE}:IdentitySpoofingCliAdapter",
        lambda command: f"/bin/{command}",
        reasoning="high",
    )

    assert [diagnostic.message for diagnostic in preview.diagnostics] == [
        "workflow 'demo' -> node 'build' -> provider 'alpha': first-class "
        "reasoning requires the built-in CLI invoker."
    ]


def test_ineligible_reasoning_does_not_construct_adapter_twice(
    tmp_path: Path,
) -> None:
    SingleConstructionInvokerAdapter.instances = 0

    preview = _compile_with_availability(
        tmp_path,
        f"{TEST_MODULE}:SingleConstructionInvokerAdapter",
        lambda command: f"/bin/{command}",
        reasoning="high",
        check_cli_availability=False,
    )

    assert SingleConstructionInvokerAdapter.instances == 1
    assert [diagnostic.message for diagnostic in preview.diagnostics] == [
        "workflow 'demo' -> node 'build' -> provider 'alpha': first-class "
        "reasoning requires the built-in CLI invoker."
    ]


def test_invalid_availability_hook_return_fails_adapter_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_availability_errors(
        adapter: CliInvokerAdapter,
        workflow: WorkflowPlan,
        config: Config,
        project_root: Path,
        executable_lookup: Callable[[str], str | None] | None = None,
    ) -> list[str]:
        del adapter, workflow, config, project_root, executable_lookup
        return ["invalid return type"]

    monkeypatch.setattr(
        CliInvokerAdapter,
        "collect_availability_errors",
        invalid_availability_errors,
    )

    with pytest.raises(
        AdapterContractError,
        match=r"collect_availability_errors\(\) must return tuple",
    ):
        _compile_with_availability(
            tmp_path,
            "cli",
            lambda command: f"/bin/{command}",
        )


def test_availability_hook_exception_fails_adapter_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_availability_errors(
        adapter: CliInvokerAdapter,
        workflow: WorkflowPlan,
        config: Config,
        project_root: Path,
        executable_lookup: Callable[[str], str | None] | None = None,
    ) -> tuple[str, ...]:
        del adapter, workflow, config, project_root, executable_lookup
        raise RuntimeError("probe failed")

    monkeypatch.setattr(
        CliInvokerAdapter,
        "collect_availability_errors",
        failing_availability_errors,
    )

    with pytest.raises(
        AdapterContractError,
        match=r"collect_availability_errors\(\) failed: probe failed",
    ):
        _compile_with_availability(
            tmp_path,
            "cli",
            lambda command: f"/bin/{command}",
        )


def test_missing_builtin_reasoning_hook_fails_adapter_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(CliInvokerAdapter, "collect_reasoning_errors", None)

    with pytest.raises(
        AdapterContractError,
        match="must define collect_reasoning_errors",
    ):
        _compile_with_availability(
            tmp_path,
            "cli",
            lambda command: f"/bin/{command}",
            reasoning="high",
        )


def test_invalid_builtin_reasoning_hook_return_fails_adapter_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_reasoning_errors(
        adapter: CliInvokerAdapter,
        workflow: WorkflowPlan,
        config: Config,
        working_directory: Path | None = None,
    ) -> list[str]:
        del adapter, workflow, config, working_directory
        return ["invalid return type"]

    monkeypatch.setattr(
        CliInvokerAdapter,
        "collect_reasoning_errors",
        invalid_reasoning_errors,
    )

    with pytest.raises(
        AdapterContractError,
        match=r"collect_reasoning_errors\(\) must return tuple",
    ):
        _compile_with_availability(
            tmp_path,
            "cli",
            lambda command: f"/bin/{command}",
            reasoning="high",
        )


def test_builtin_reasoning_hook_exception_fails_adapter_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_reasoning_errors(
        adapter: CliInvokerAdapter,
        workflow: WorkflowPlan,
        config: Config,
        working_directory: Path | None = None,
    ) -> tuple[str, ...]:
        del adapter, workflow, config, working_directory
        raise RuntimeError("reasoning validation failed")

    monkeypatch.setattr(
        CliInvokerAdapter,
        "collect_reasoning_errors",
        failing_reasoning_errors,
    )

    with pytest.raises(
        AdapterContractError,
        match=r"collect_reasoning_errors\(\) failed: reasoning validation failed",
    ):
        _compile_with_availability(
            tmp_path,
            "cli",
            lambda command: f"/bin/{command}",
            reasoning="high",
        )
