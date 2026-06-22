import pytest
from pydantic import ValidationError

from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight.plan_contract import (
    validate_supported_plan_schema_version,
)
from orchestrator_cli.core.workflow_models import WorkflowPlan
from orchestrator_cli.version import SCHEMA_VERSION


def test_authored_schema_version_remains_current() -> None:
    assert SCHEMA_VERSION == "1.0"


def test_accidental_next_major_schema_version_is_not_supported() -> None:
    with pytest.raises(ValidationError, match="Unsupported config version '2.0'"):
        Config.model_validate({"version": "2.0", "agents": {}})

    with pytest.raises(
        ValidationError,
        match="Unsupported workflow schema version '2.0'",
    ):
        WorkflowPlan.model_validate(
            {"schema_version": "2.0", "name": "demo", "nodes": []}
        )

    with pytest.raises(
        ValueError, match="Unsupported preflight plan schema version '2.0'"
    ):
        validate_supported_plan_schema_version("2.0")
