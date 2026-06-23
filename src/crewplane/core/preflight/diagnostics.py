from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PreflightDiagnosticSeverity = Literal["error", "warning"]


class PreflightDiagnosticCode(StrEnum):
    DAG = "DAG"
    FILE_ENCODING = "FILE-ENCODING"
    FILE_POLICY = "FILE-POLICY"
    FINGERPRINT_KEY = "FINGERPRINT-KEY"
    INPUT_SOURCE = "INPUT-SOURCE"
    NODE_REFERENCE = "NODE-REFERENCE"
    PARAM_TOKEN = "PARAM-TOKEN"
    PREFLIGHT_SETUP = "PREFLIGHT-SETUP"
    PREFLIGHT_VALIDATION = "PREFLIGHT-VALIDATION"
    PROVIDER_CLI = "PROVIDER-CLI"
    PROVIDER_CONFIG = "PROVIDER-CONFIG"
    RUNTIME_CONFIG = "RUNTIME-CONFIG"
    TEMPLATE_PLAN = "TEMPLATE-PLAN"
    TEMPLATE_TOKEN = "TEMPLATE-TOKEN"
    TEMPLATE_VALUE = "TEMPLATE-VALUE"
    WORKSPACE_FILE_LOCATOR = "WORKSPACE-FILE-LOCATOR"
    WORKSPACE_GIT_CONTRACT = "WORKSPACE-GIT-CONTRACT"
    WORKSPACE_INVOKER = "WORKSPACE-INVOKER"
    WORKSPACE_RUNTIME = "WORKSPACE-RUNTIME"
    WORKSPACE_SOURCE = "WORKSPACE-SOURCE"


class PreflightDiagnosticPhase(StrEnum):
    ENV_POLICY = "env_policy"
    FILE_POLICY = "file_policy"
    INVOKER_WORKSPACE_COMPATIBILITY = "invoker_workspace_compatibility"
    NODE_POLICY = "node_policy"
    PARSE = "parse"
    PROVIDER = "provider"
    REFERENCE = "reference"
    SOURCE_POLICY = "source_policy"
    TEMPLATE_PLAN = "template_plan"
    VALIDATION = "validation"
    VAR_POLICY = "var_policy"
    WORKSPACE_FILE_LOCATOR_POLICY = "workspace_file_locator_policy"
    WORKSPACE_POLICY = "workspace_policy"
    WORKTREE_CONTRACT = "worktree_contract"


class PreflightDiagnostic(BaseModel):
    """A deterministic preflight diagnostic safe to persist."""

    model_config = ConfigDict(extra="forbid")

    code: PreflightDiagnosticCode
    message: str
    severity: PreflightDiagnosticSeverity = "error"
    phase: PreflightDiagnosticPhase
    node_id: str | None = None
    path: str | None = None
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)
