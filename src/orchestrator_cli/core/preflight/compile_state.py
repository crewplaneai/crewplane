from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from orchestrator_cli.core.workflow_models import WorkflowNode

from .diagnostics import PreflightDiagnostic
from .models import DependencyEdge, StaticResource, TokenCatalogEntry
from .references import TemplateReference
from .secrets import FingerprintKeyCache, FingerprintKeyPolicy, SecretContext
from .source import PreflightWorkflowSource

_SAFE_ARTIFACT_PATTERN = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ResolvedStaticFileReference:
    resource: StaticResource
    token_signature: str


@dataclass(frozen=True)
class ResolvedStaticValueReference:
    kind: Literal["env", "var"]
    key: str
    sensitive: bool
    value_handle: str | None
    value_stored: str | None
    fingerprint: str | None
    token_signature: str


@dataclass(frozen=True)
class PreflightCompileOptions:
    project_root: Path
    orchestrator_dir: Path
    node_source_roots: dict[str, Path] = field(default_factory=dict)
    node_source_files: dict[str, Path] = field(default_factory=dict)
    node_source_spans: dict[str, dict[str, int]] = field(default_factory=dict)
    prompt_segment_spans: dict[str, tuple[dict[str, int], ...]] = field(
        default_factory=dict
    )
    allowed_template_paths: tuple[Path, ...] = ()
    runtime_variables: dict[str, str] = field(default_factory=dict)
    environment: dict[str, str] | None = None
    fingerprint_key_policy: FingerprintKeyPolicy = "read_only"
    fingerprint_key_cache: FingerprintKeyCache = field(
        default_factory=FingerprintKeyCache
    )
    additional_validation_errors: tuple[str, ...] = ()
    additional_validation_warnings: tuple[str, ...] = ()

    def with_source_metadata(
        self,
        source: PreflightWorkflowSource,
    ) -> PreflightCompileOptions:
        root_source_path = (
            source.referenced_workflows[0].path.resolve(strict=False)
            if source.referenced_workflows
            else None
        )
        node_source_files = {
            node_id: source_path.resolve(strict=False)
            for node_id, source_path in source.node_source_paths.items()
        }
        return replace(
            self,
            node_source_roots={
                node_id: _source_root_for_node(
                    source_path,
                    root_source_path,
                    self.project_root,
                )
                for node_id, source_path in node_source_files.items()
            },
            node_source_files=node_source_files,
            node_source_spans={
                node_id: dict(span)
                for node_id, span in source.node_source_spans.items()
            },
            prompt_segment_spans={
                node_id: tuple(spans)
                for node_id, spans in source.prompt_segment_spans.items()
            },
        )


def _source_root_for_node(
    source_path: Path,
    root_source_path: Path | None,
    project_root: Path,
) -> Path:
    if root_source_path is not None and source_path == root_source_path:
        return project_root.resolve(strict=False)
    return source_path.parent.resolve(strict=False)


@dataclass
class CompileState:
    diagnostics: list[PreflightDiagnostic] = field(default_factory=list)
    diagnostic_keys: set[tuple[str, str, str | None, str | None, str]] = field(
        default_factory=set
    )
    token_catalog: list[TokenCatalogEntry] = field(default_factory=list)
    static_resources: list[StaticResource] = field(default_factory=list)
    static_payloads: dict[str, bytes] = field(default_factory=dict)
    static_file_references: dict[str, ResolvedStaticFileReference] = field(
        default_factory=dict
    )
    static_value_references: dict[str, ResolvedStaticValueReference] = field(
        default_factory=dict
    )
    dependency_edges: dict[tuple[str, str, str | None], DependencyEdge] = field(
        default_factory=dict
    )
    secret_context: SecretContext = field(default_factory=SecretContext)
    sensitive_values_required: bool = False
    fingerprint_key: bytes | None = None
    fingerprint_key_persisted: bool = False
    value_fingerprints: list[dict[str, str]] = field(default_factory=list)
    input_content_refs: dict[str, str] = field(default_factory=dict)
    input_source_tokens: dict[str, TemplateReference] = field(default_factory=dict)
    render_token_index: int = 0


def append_multiline_diagnostics(state: CompileState, phase: str, message: str) -> None:
    for line in message.splitlines():
        if not line.strip():
            continue
        append_diagnostic(
            state,
            code="PREFLIGHT-VALIDATION",
            phase=phase,
            message=line,
        )


def has_errors(state: CompileState) -> bool:
    return any(diagnostic.severity == "error" for diagnostic in state.diagnostics)


def append_diagnostic(
    state: CompileState,
    code: str,
    phase: str,
    message: str,
    node_id: str | None = None,
    path: str | None = None,
    severity: Literal["error", "warning"] = "error",
) -> None:
    diagnostic = PreflightDiagnostic(
        code=code,
        phase=phase,
        node_id=node_id,
        path=path,
        message=message,
        severity=severity,
    )
    key = diagnostic_key(diagnostic)
    if key in state.diagnostic_keys:
        return
    state.diagnostic_keys.add(key)
    state.diagnostics.append(diagnostic)


def extend_diagnostics(
    state: CompileState,
    diagnostics: tuple[PreflightDiagnostic, ...],
) -> None:
    for diagnostic in diagnostics:
        key = diagnostic_key(diagnostic)
        if key in state.diagnostic_keys:
            continue
        state.diagnostic_keys.add(key)
        state.diagnostics.append(diagnostic)


def diagnostic_key(
    diagnostic: PreflightDiagnostic,
) -> tuple[str, str, str | None, str | None, str]:
    return (
        diagnostic.code,
        diagnostic.phase,
        diagnostic.node_id,
        diagnostic.path,
        diagnostic.message,
    )


def module_id(node: WorkflowNode, options: PreflightCompileOptions) -> str | None:
    source_file = options.node_source_files.get(node.id)
    if source_file is None:
        return None
    resolved_source = source_file.resolve(strict=False)
    try:
        return resolved_source.relative_to(options.project_root).as_posix()
    except ValueError:
        return resolved_source.as_posix()


def source_root(node: WorkflowNode, options: PreflightCompileOptions) -> Path:
    return options.node_source_roots.get(node.id, options.project_root).resolve(
        strict=False
    )


def source_file(node: WorkflowNode, options: PreflightCompileOptions) -> str | None:
    source_path = options.node_source_files.get(node.id)
    return source_path.as_posix() if source_path is not None else None


def node_source_span(
    node: WorkflowNode,
    options: PreflightCompileOptions,
) -> dict[str, int] | None:
    span = options.node_source_spans.get(node.id)
    return dict(span) if span is not None else None


def token_source_span(
    node: WorkflowNode,
    options: PreflightCompileOptions,
    segment_index: int,
    reference: TemplateReference,
) -> dict[str, int] | None:
    segment_span = prompt_segment_span(node, options, segment_index)
    if segment_span is None:
        return None
    segment_content = node.prompt_segments[segment_index].content
    prefix = segment_content[: reference.start]
    end_prefix = segment_content[: reference.end]
    return {
        "start_line": segment_span["start_line"] + prefix.count("\n"),
        "start_column": column_at_offset(prefix),
        "end_line": segment_span["start_line"] + end_prefix.count("\n"),
        "end_column": column_at_offset(end_prefix),
    }


def prompt_segment_span(
    node: WorkflowNode,
    options: PreflightCompileOptions,
    segment_index: int,
) -> dict[str, int] | None:
    spans = options.prompt_segment_spans.get(node.id, ())
    if segment_index >= len(spans):
        return None
    return dict(spans[segment_index])


def column_at_offset(text: str) -> int:
    return len(text.rsplit("\n", 1)[-1])


def allowed_template_paths(options: PreflightCompileOptions) -> tuple[Path, ...]:
    return tuple(
        path.expanduser().resolve(strict=False)
        for path in options.allowed_template_paths
    )


def safe_artifact_name(name: str) -> str:
    slug = _SAFE_ARTIFACT_PATTERN.sub("-", name.strip().lower()).strip("-")
    return slug or "task"
