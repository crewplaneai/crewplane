from __future__ import annotations

import pytest

from tests.integration.architecture.static_checks import (
    REPO_ROOT,
    SRC_ROOT,
    ForbiddenImportRule,
    find_forbidden_imports,
)

IMPORT_RULES = (
    ForbiddenImportRule(
        name="adapters stay independent from runtime execution",
        roots=(SRC_ROOT / "crewplane" / "adapters",),
        forbidden_prefixes=("crewplane.runtime.execution",),
    ),
    ForbiddenImportRule(
        name="architecture ports stay runtime neutral",
        roots=(SRC_ROOT / "crewplane" / "architecture" / "ports",),
        forbidden_prefixes=(
            "crewplane.core.preflight.runtime_config",
            "crewplane.runtime",
            "crewplane.observability",
        ),
    ),
    ForbiddenImportRule(
        name="architecture contracts stay below runtime and adapters",
        roots=(SRC_ROOT / "crewplane" / "architecture" / "contracts",),
        forbidden_prefixes=(
            "crewplane.adapters",
            "crewplane.observability",
            "crewplane.runtime",
        ),
    ),
    ForbiddenImportRule(
        name="runtime execution stays independent from workflow authoring models",
        roots=(SRC_ROOT / "crewplane" / "runtime" / "execution",),
        forbidden_prefixes=("crewplane.core.workflow.models",),
    ),
    ForbiddenImportRule(
        name="review contract stays core neutral",
        roots=(SRC_ROOT / "crewplane" / "core" / "review_contract.py",),
        forbidden_prefixes=(
            "crewplane.runtime",
            "crewplane.adapters",
            "crewplane.artifacts",
            "crewplane.observability",
        ),
    ),
    ForbiddenImportRule(
        name="log presentation stays independent from runtime parsing",
        roots=(SRC_ROOT / "crewplane" / "observability" / "log_presentation",),
        forbidden_prefixes=(
            "crewplane.runtime.agent.invocation.output",
            "crewplane.runtime.agent.invocation.claude_json",
            "crewplane.runtime.agent.usage_parsing",
            "crewplane.runtime.agent.quota",
            "crewplane.runtime.agent.failures",
        ),
    ),
    ForbiddenImportRule(
        name="runtime agent stays provider neutral",
        roots=(SRC_ROOT / "crewplane" / "runtime" / "agent",),
        forbidden_prefixes=(
            "crewplane.adapters.invokers.cli_invoker.machine_json",
            "crewplane.adapters.invokers.cli_invoker.usage_decoders",
        ),
    ),
)


@pytest.mark.parametrize("rule", IMPORT_RULES, ids=lambda rule: rule.name)
def test_forbidden_import_rule(rule: ForbiddenImportRule) -> None:
    assert find_forbidden_imports(rule) == []


def test_import_rule_roots_exist() -> None:
    missing = [
        str(root.relative_to(REPO_ROOT))
        for rule in IMPORT_RULES
        for root in rule.roots
        if not root.exists()
    ]
    assert missing == []
