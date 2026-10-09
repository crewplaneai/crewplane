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
            "crewplane.adapters.invokers.cli_invoker",
        ),
    ),
    ForbiddenImportRule(
        name="runtime agent stays provider neutral",
        roots=(SRC_ROOT / "crewplane" / "runtime" / "agent",),
        forbidden_prefixes=("crewplane.adapters.invokers.cli_invoker",),
    ),
    ForbiddenImportRule(
        name="run preflight dispatches invoker diagnostics through adapters",
        roots=(SRC_ROOT / "crewplane" / "cli" / "run" / "preflight.py",),
        forbidden_prefixes=("crewplane.adapters.invokers",),
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


def _production_files_except(*owners: str):
    allowed = {SRC_ROOT / "crewplane" / owner for owner in owners}
    return tuple(
        path
        for path in sorted((SRC_ROOT / "crewplane").rglob("*.py"))
        if path not in allowed
    )


PLATFORM_IMPORT_RULES = (
    ForbiddenImportRule(
        name="concrete command strategies stay behind adapter selection",
        roots=_production_files_except(
            "adapters/invokers/cli_invoker/command_strategy.py"
        ),
        forbidden_prefixes=(
            "crewplane.adapters.invokers.cli_invoker.command_posix",
            "crewplane.adapters.invokers.cli_invoker.command_windows",
        ),
    ),
    ForbiddenImportRule(
        name="launcher parsing stays in the Windows command implementation",
        roots=_production_files_except(
            "adapters/invokers/cli_invoker/command_windows.py"
        ),
        forbidden_prefixes=(
            "crewplane.adapters.invokers.cli_invoker.windows_launchers",
        ),
    ),
    ForbiddenImportRule(
        name="process implementations stay behind session selection",
        roots=_production_files_except("runtime/agent/process/session.py"),
        forbidden_prefixes=(
            "crewplane.runtime.agent.process.posix_session",
            "crewplane.runtime.agent.process.windows_launch",
        ),
    ),
    ForbiddenImportRule(
        name="native jobs stay owned by the Windows session",
        roots=_production_files_except("runtime/agent/process/windows_launch.py"),
        forbidden_prefixes=("crewplane.runtime.agent.process.windows_job",),
    ),
    ForbiddenImportRule(
        name="native filesystem resources stay in selected I/O owners",
        roots=_production_files_except(
            "architecture/safe_file_operations.py",
            "architecture/safe_files_windows.py",
            "artifacts/atomic_windows.py",
            "artifacts/generated_files/io_windows.py",
            "runtime/execution/provider_call/provider_output_windows.py",
            "runtime/workspace/snapshot_scan_windows.py",
            "observability/run_summary/event_log_windows.py",
        ),
        forbidden_prefixes=(
            "crewplane.architecture.safe_files_windows",
            "crewplane.architecture.windows_file_handles",
        ),
    ),
    ForbiddenImportRule(
        name="POSIX safe-file operations stay behind shared selection",
        roots=_production_files_except("architecture/safe_file_operations.py"),
        forbidden_prefixes=("crewplane.architecture.safe_files_posix",),
    ),
    ForbiddenImportRule(
        name="snapshot implementations depend on common definitions",
        roots=tuple(
            SRC_ROOT / "crewplane/runtime/workspace" / name
            for name in (
                "snapshot_scan_posix.py",
                "snapshot_scan_windows.py",
                "snapshot_scan_common.py",
            )
        ),
        forbidden_prefixes=("crewplane.runtime.workspace.snapshot_scan",),
    ),
    ForbiddenImportRule(
        name="artifacts do not interpret runtime cleanup exceptions",
        roots=(SRC_ROOT / "crewplane/artifacts",),
        forbidden_prefixes=("crewplane.runtime",),
    ),
)


@pytest.mark.parametrize("rule", PLATFORM_IMPORT_RULES, ids=lambda rule: rule.name)
def test_platform_ownership_import_rule(rule: ForbiddenImportRule) -> None:
    assert rule.roots
    assert find_forbidden_imports(rule) == []
