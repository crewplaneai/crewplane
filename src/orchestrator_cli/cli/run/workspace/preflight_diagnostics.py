from __future__ import annotations

from orchestrator_cli.core.preflight.diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
    PreflightDiagnosticSeverity,
)

from .source_types import WorkspacePolicyCheck


def workspace_preflight_diagnostics(
    workspace_check: WorkspacePolicyCheck,
) -> tuple[PreflightDiagnostic, ...]:
    return (
        *(
            workspace_preflight_diagnostic(message, "error")
            for message in workspace_check.errors
        ),
        *(
            workspace_preflight_diagnostic(message, "warning")
            for message in workspace_check.warnings
        ),
    )


def workspace_preflight_diagnostic(
    message: str,
    severity: PreflightDiagnosticSeverity,
) -> PreflightDiagnostic:
    code, phase = workspace_diagnostic_code_and_phase(message)
    return PreflightDiagnostic(
        code=code,
        phase=phase,
        message=message,
        severity=severity,
    )


def workspace_diagnostic_code_and_phase(
    message: str,
) -> tuple[PreflightDiagnosticCode, PreflightDiagnosticPhase]:
    if message.startswith("Workspace invoker compatibility failed"):
        return (
            PreflightDiagnosticCode.WORKSPACE_INVOKER,
            PreflightDiagnosticPhase.INVOKER_WORKSPACE_COMPATIBILITY,
        )
    if message.startswith("Workspace Git contract failed"):
        return (
            PreflightDiagnosticCode.WORKSPACE_GIT_CONTRACT,
            PreflightDiagnosticPhase.WORKTREE_CONTRACT,
        )
    return (
        PreflightDiagnosticCode.WORKSPACE_SOURCE,
        PreflightDiagnosticPhase.SOURCE_POLICY,
    )
