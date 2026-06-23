from __future__ import annotations

import pytest
from pydantic import ValidationError

from crewplane.core.preflight import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from crewplane.core.preflight.fragment_handlers import static_value_phase


def test_preflight_diagnostic_code_serializes_as_stable_string() -> None:
    diagnostic = PreflightDiagnostic(
        code=PreflightDiagnosticCode.TEMPLATE_PLAN,
        phase="template_plan",
        message="Token could not be planned.",
    )

    assert diagnostic.code is PreflightDiagnosticCode.TEMPLATE_PLAN
    assert diagnostic.code == "TEMPLATE-PLAN"
    assert diagnostic.model_dump(mode="json")["code"] == "TEMPLATE-PLAN"


def test_preflight_diagnostic_accepts_existing_string_code_values() -> None:
    diagnostic = PreflightDiagnostic(
        code="FILE-ENCODING",
        phase="file_policy",
        message="File token content must be UTF-8 text.",
    )

    assert diagnostic.code is PreflightDiagnosticCode.FILE_ENCODING
    assert diagnostic.model_dump(mode="json")["code"] == "FILE-ENCODING"


def test_preflight_diagnostic_rejects_unknown_code_values() -> None:
    with pytest.raises(ValidationError):
        PreflightDiagnostic(
            code="FILE-ENCONDING",
            phase="file_policy",
            message="Typo should fail validation.",
        )


def test_preflight_diagnostic_phase_serializes_as_stable_string() -> None:
    diagnostic = PreflightDiagnostic(
        code=PreflightDiagnosticCode.TEMPLATE_PLAN,
        phase=PreflightDiagnosticPhase.TEMPLATE_PLAN,
        message="Token could not be planned.",
    )

    assert diagnostic.phase is PreflightDiagnosticPhase.TEMPLATE_PLAN
    assert diagnostic.phase == "template_plan"
    assert diagnostic.model_dump(mode="json")["phase"] == "template_plan"


def test_preflight_diagnostic_accepts_existing_string_phase_values() -> None:
    diagnostic = PreflightDiagnostic(
        code=PreflightDiagnosticCode.FILE_ENCODING,
        phase="file_policy",
        message="File token content must be UTF-8 text.",
    )

    assert diagnostic.phase is PreflightDiagnosticPhase.FILE_POLICY
    assert diagnostic.model_dump(mode="json")["phase"] == "file_policy"


def test_preflight_diagnostic_rejects_unknown_phase_values() -> None:
    with pytest.raises(ValidationError):
        PreflightDiagnostic(
            code=PreflightDiagnosticCode.FILE_ENCODING,
            phase="file_polciy",
            message="Typo should fail validation.",
        )


def test_static_value_phase_rejects_unsupported_token_kind() -> None:
    assert static_value_phase("env") is PreflightDiagnosticPhase.ENV_POLICY
    assert static_value_phase("var") is PreflightDiagnosticPhase.VAR_POLICY

    with pytest.raises(ValueError, match="Unsupported static value token kind"):
        static_value_phase("param")
