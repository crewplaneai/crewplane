"""Preflight compiler contracts for compiled workflow execution."""

from .compiler import PreflightCompileOptions, compile_preflight_preview
from .diagnostics import PreflightDiagnostic
from .models import (
    PREFLIGHT_STATUS_FAILED,
    PREFLIGHT_STATUS_SUCCEEDED,
    ArtifactContract,
    DependencyEdge,
    Fragment,
    PreflightCompilationPreview,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
    RenderStream,
    StaticResource,
    TokenCatalogEntry,
)
from .runner import load_workflow_source_for_preflight
from .runtime_config import (
    CanonicalIntegrationConfig,
    RuntimeConfigSnapshot,
    RuntimeConfigSnapshotOptions,
)
from .secrets import FingerprintKeyProvider, SecretContext
from .serialization import canonical_json, canonical_json_bytes
from .signatures import signature_for_payload
from .source import PreflightWorkflowSource
from .variables import build_builtin_template_variables

__all__ = [
    "ArtifactContract",
    "CanonicalIntegrationConfig",
    "DependencyEdge",
    "FingerprintKeyProvider",
    "Fragment",
    "PREFLIGHT_STATUS_FAILED",
    "PREFLIGHT_STATUS_SUCCEEDED",
    "PreflightCompilationPreview",
    "PreflightCompileOptions",
    "PreflightDiagnostic",
    "PreflightExecutionNode",
    "PreflightExecutionPlan",
    "PreflightWorkflowSource",
    "ProviderRecord",
    "RenderPlan",
    "RenderStream",
    "RuntimeConfigSnapshot",
    "RuntimeConfigSnapshotOptions",
    "SecretContext",
    "StaticResource",
    "TokenCatalogEntry",
    "build_builtin_template_variables",
    "canonical_json",
    "canonical_json_bytes",
    "compile_preflight_preview",
    "load_workflow_source_for_preflight",
    "signature_for_payload",
]
