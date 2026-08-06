from __future__ import annotations

import pytest

from crewplane.cli.run.preflight import uses_cli_invoker, uses_mock_invoker
from crewplane.core.config import (
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from crewplane.version import SCHEMA_VERSION


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
