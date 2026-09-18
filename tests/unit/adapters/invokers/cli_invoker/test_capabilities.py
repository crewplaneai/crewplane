from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker import capabilities
from crewplane.adapters.invokers.cli_invoker.capability import (
    CliCommand,
    CliInvocationRequest,
    CliProviderCapability,
)
from crewplane.adapters.invokers.cli_invoker.commands import build_standard_command
from crewplane.adapters.invokers.cli_invoker.failures.classifier import (
    classify_generic_failure,
)
from crewplane.adapters.invokers.cli_invoker.providers import codex
from crewplane.adapters.invokers.cli_invoker.quota.classifier import (
    classify_generic_quota,
)
from crewplane.adapters.invokers.cli_invoker.validation import (
    reject_unsupported_reasoning,
)
from crewplane.architecture.contracts import InvocationContext, ProviderKind
from crewplane.core.config import AgentConfig


def test_capability_defaults_are_explicit_and_immutable() -> None:
    capability = CliProviderCapability(
        provider_kind=ProviderKind.GENERIC,
        validate_request=reject_unsupported_reasoning,
        build_command=build_standard_command,
        output_extractor=None,
    )
    assert capability.quota_classifier is classify_generic_quota
    assert capability.failure_classifier is classify_generic_failure
    assert capability.usage_decoder is None
    assert capability.one_shot_failure_retry is None
    assert capability.log_presentation_format == "plain"
    assert capability.log_presentation_profile == "generic"
    assert capability.supports_output_idle_timeout
    with pytest.raises(FrozenInstanceError):
        capability.supports_output_idle_timeout = False


def test_registry_is_complete_and_missing_registration_fails(monkeypatch) -> None:
    assert set(capabilities.CAPABILITIES) == set(ProviderKind)
    for kind in ProviderKind:
        assert capabilities.get_cli_provider_capability(kind).provider_kind == kind
    monkeypatch.delitem(capabilities.CAPABILITIES, ProviderKind.GENERIC)
    with pytest.raises(ValueError, match="No CLI capability registered"):
        capabilities.get_cli_provider_capability(ProviderKind.GENERIC)


def test_plan_assembly_failure_releases_returned_command_output(
    tmp_path, monkeypatch
) -> None:
    owned = tmp_path / "owned.txt"

    def build_command(request: CliInvocationRequest, prompt: str) -> CliCommand:
        assert request.config.provider_kind == ProviderKind.CODEX
        owned.write_text(prompt)
        return CliCommand(["codex"], None, owned)

    def fail_header(**kwargs) -> bytes:
        assert kwargs["cli_executable"] == "codex"
        raise RuntimeError("header failed")

    monkeypatch.setitem(
        capabilities.CAPABILITIES,
        ProviderKind.CODEX,
        replace(
            capabilities.CAPABILITIES[ProviderKind.CODEX], build_command=build_command
        ),
    )
    monkeypatch.setattr(capabilities, "build_provider_log_header", fail_header)
    with pytest.raises(RuntimeError, match="header failed"):
        capabilities.build_cli_invocation_plan(
            AgentConfig(cli_cmd=["codex"], provider_kind="codex"),
            None,
            "hello",
            Path("out.txt"),
        )
    assert not owned.exists()


def test_invalid_reasoning_is_rejected_before_codex_allocation(
    tmp_path, monkeypatch
) -> None:
    def unexpected_allocation(**kwargs):
        raise AssertionError(f"validation allocated a file: {kwargs}")

    monkeypatch.setattr(codex.tempfile, "mkstemp", unexpected_allocation)
    context = InvocationContext(
        "node", "task", "codex", "executor", requested_reasoning="high"
    )
    with pytest.raises(ValueError, match="conflicts"):
        capabilities.build_cli_invocation_plan(
            AgentConfig(
                cli_cmd=["codex", "-c", "model_reasoning_effort=low"],
                provider_kind="codex",
            ),
            None,
            "hello",
            tmp_path / "out.txt",
            context,
        )


def test_failing_codex_builder_releases_its_allocation(tmp_path, monkeypatch) -> None:
    allocated = []
    mkstemp = codex.tempfile.mkstemp

    def allocate(**kwargs):
        descriptor, name = mkstemp(dir=tmp_path, **kwargs)
        allocated.append(Path(name))
        return descriptor, name

    def fail_command(request, prompt, structured_args, reasoning_args):
        assert request.requested_reasoning is None
        assert reasoning_args == ()
        Path(structured_args[-1]).write_text(prompt)
        raise RuntimeError("command failed")

    monkeypatch.setattr(codex.tempfile, "mkstemp", allocate)
    monkeypatch.setattr(codex, "build_standard_command", fail_command)
    with pytest.raises(RuntimeError, match="command failed"):
        capabilities.build_cli_invocation_plan(
            AgentConfig(cli_cmd=["codex"], provider_kind="codex"),
            None,
            "hello",
            tmp_path / "out.txt",
        )
    assert len(allocated) == 1
    assert not allocated[0].exists()
