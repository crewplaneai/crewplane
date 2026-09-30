from __future__ import annotations

import asyncio
import inspect

import pytest

from crewplane.architecture.contracts.integration import CanonicalIntegrationConfig
from crewplane.core.preflight.models import PreflightExecutionPlan, ProviderRecord
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.serialization import canonical_json_bytes
from crewplane.core.preflight.signatures import signature_for_payload
from crewplane.runtime.execution.deferred_cleanup import DeferredAsyncCleanupRegistry
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
    agent_config_payload_from_plan,
    agent_config_signature_from_plan,
    invoker_config_signature_from_plan,
    resolve_secret_config_values,
)
from tests.helpers.resume import make_plan


def plan_with_runtime_metadata(metadata: dict[str, object]) -> PreflightExecutionPlan:
    payload = make_plan().model_dump(mode="json")
    payload["runtime_config_snapshot"].update(metadata)
    return PreflightExecutionPlan.model_validate(payload)


@pytest.mark.parametrize(
    "agents", [None, [], {"alpha": None}, {"other": {"cli_cmd": ["local"]}}]
)
def test_runtime_rejects_missing_agent_configuration(agents: object) -> None:
    plan = plan_with_runtime_metadata({"agents": agents})
    with pytest.raises(
        ValueError, match="missing runtime agent config|missing agent config"
    ):
        agent_config_payload_from_plan(plan, "alpha")


@pytest.mark.parametrize(
    "invoker",
    [
        None,
        [],
        {"options": None, "option_scopes": {}},
        {"options": {}, "option_scopes": None},
    ],
)
def test_runtime_does_not_sign_incomplete_invoker_metadata(invoker: object) -> None:
    plan = plan_with_runtime_metadata({"invoker": invoker})
    assert invoker_config_signature_from_plan(plan) is None


def test_execution_contract_rejects_unsigned_provider_configuration() -> None:
    context = CompiledRuntimeContext(make_plan(), SecretContext())
    with pytest.raises(ValueError, match="missing signed agent config"):
        context.validate_execution_contract()
    assert context.max_concurrent_nodes() is None
    assert context.max_parallel_invocations() is None


@pytest.mark.parametrize("invoker", [None, {"options": {}, "option_scopes": {}}])
def test_runtime_rejects_missing_or_mismatched_invoker_signature(
    invoker: object,
) -> None:
    plan = plan_with_runtime_metadata(
        {"agents": {"alpha": {"cli_cmd": ["local"]}}, "invoker": invoker}
    )
    provider_payload = plan.nodes[0].provider_records[0].model_dump(mode="json")
    provider_payload["agent_config_signature"] = agent_config_signature_from_plan(
        plan, "alpha", None
    )
    provider = ProviderRecord.model_validate(provider_payload)
    context = CompiledRuntimeContext(plan, SecretContext())
    with pytest.raises(
        ValueError,
        match="missing signed invoker|invoker config signature does not match",
    ):
        context.agent_config_for_provider(provider)


@pytest.mark.parametrize(
    "payload",
    [
        {"redacted": True},
        {"redacted": True, "value_handle": 3},
        {"redacted": True, "value_handle": "unavailable"},
    ],
)
def test_runtime_rejects_unresolvable_secret_configuration(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="missing a secret handle|is unavailable"):
        resolve_secret_config_values({"extra_args": [payload]}, SecretContext())


class CleanupInterrupted(BaseException):
    pass


@pytest.mark.parametrize(
    "failure", [asyncio.CancelledError(), CleanupInterrupted("cleanup interrupted")]
)
def test_cleanup_registry_reports_cancelled_and_base_exception_tasks(
    failure: BaseException,
) -> None:
    async def scenario() -> None:
        registry = DeferredAsyncCleanupRegistry()

        async def cleanup() -> None:
            raise failure

        registry.register(cleanup())
        errors = await registry.drain(1)
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert str(errors[0]) == (
            "Deferred cleanup task was cancelled."
            if isinstance(failure, asyncio.CancelledError)
            else "cleanup interrupted"
        )
        assert registry.tasks == set()

    asyncio.run(scenario())


def test_closed_cleanup_registry_closes_new_cancellable_coroutine() -> None:
    async def scenario() -> None:
        registry = DeferredAsyncCleanupRegistry()

        async def cleanup() -> None:
            await asyncio.Event().wait()

        registry.register(cleanup())
        errors = await registry.drain(0)
        assert len(errors) == 1
        assert isinstance(errors[0], TimeoutError)
        rejected = cleanup()
        registry.register(rejected)
        assert inspect.getcoroutinestate(rejected) == inspect.CORO_CLOSED
        assert registry.tasks == set()

    asyncio.run(scenario())


@pytest.mark.parametrize("scope_mask", range(16))
def test_integration_projection_preserves_scope_selection_and_runtime_signature(
    scope_mask: int,
) -> None:

    scopes = ("execution", "artifact", "observer", "validation")
    options = {
        scope: {"nested": [None, True, {"redacted": True, "fingerprint": "abc"}]}
        for scope in scopes
    }
    config = CanonicalIntegrationConfig(
        implementation="custom",
        resolved_identity="example.Adapter",
        capabilities={"cwd": True},
        options=options,
        option_scopes={scope: scope for scope in scopes},
    )
    selected = {
        scope for index, scope in enumerate(scopes) if scope_mask & (1 << index)
    }
    expected = {
        "capabilities": {"cwd": True},
        "implementation": "custom",
        "options": {scope: options[scope] for scope in scopes if scope in selected},
        "resolved_identity": "example.Adapter",
    }
    assert config.scoped_payload(selected) == expected
    plan = plan_with_runtime_metadata({"invoker": config.redacted_payload()})
    assert invoker_config_signature_from_plan(plan) == signature_for_payload(
        config.scoped_payload({"execution", "artifact"})
    )


@pytest.mark.parametrize("scope", ["execution", "artifact", "observer", "validation"])
def test_runtime_integration_signature_changes_only_for_execution_and_artifact(
    scope: str,
) -> None:

    config = CanonicalIntegrationConfig(
        implementation="custom",
        resolved_identity="example.Adapter",
        options={"option": {"nested": [1]}},
        option_scopes={"option": scope},
    )
    original = signature_for_payload(config.scoped_payload({"execution", "artifact"}))
    payload = config.redacted_payload()
    payload["options"]["option"]["nested"] = [2]
    changed = invoker_config_signature_from_plan(
        plan_with_runtime_metadata({"invoker": payload})
    )
    assert (changed != original) is (scope in {"execution", "artifact"})


@pytest.mark.parametrize("capability_metadata", [{}, {"capabilities": None}])
def test_runtime_projection_preserves_missing_identity_and_null_capabilities(
    capability_metadata: dict[str, object],
) -> None:

    invoker = {
        "options": {"unscoped": "ignored", "nested": {"redacted": True}},
        "option_scopes": {"nested": "execution", "absent": "artifact"},
        **capability_metadata,
    }
    expected = {
        "capabilities": capability_metadata.get("capabilities", {}),
        "implementation": None,
        "options": {"nested": {"redacted": True}},
        "resolved_identity": None,
    }
    capability_bytes = b"null" if capability_metadata else b"{}"
    assert canonical_json_bytes(expected) == (
        b'{"capabilities":'
        + capability_bytes
        + b',"implementation":null,"options":{"nested":{"redacted":true}},'
        b'"resolved_identity":null}'
    )
    assert invoker_config_signature_from_plan(
        plan_with_runtime_metadata({"invoker": invoker})
    ) == signature_for_payload(expected)
