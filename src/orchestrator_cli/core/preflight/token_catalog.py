from __future__ import annotations

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.core.prompt_segments import PromptSegmentRole
from orchestrator_cli.core.workflow_models import WorkflowNode

from .compile_state import CompileState, PreflightCompileOptions, source_file
from .models import TokenCatalogEntry
from .references import TemplateReference


def append_token_catalog(
    state: CompileState,
    occurrence_id: str,
    node: WorkflowNode,
    target_role: str,
    source_role: PromptSegmentRole,
    reference: TemplateReference,
    token_kind: str,
    fragment_index: int,
    signature: str,
    metadata: dict[str, str],
    options: PreflightCompileOptions,
    canonical_locator: str | None = None,
    dependency_signature: str | None = None,
    source_span: dict[str, int] | None = None,
    resolved: JsonObject | None = None,
) -> None:
    state.token_catalog.append(
        TokenCatalogEntry(
            occurrence_id=occurrence_id,
            node_id=node.id,
            target_role=target_role,
            source_role=source_role,
            raw_token=reference.raw_token,
            token_kind=token_kind,
            fragment_index=fragment_index,
            signature=signature,
            source_file=source_file(node, options),
            source_span=source_span,
            token_raw_span={"start": reference.start, "end": reference.end},
            canonical_locator=canonical_locator,
            dependency_signature=dependency_signature,
            resolved=resolved or {},
            metadata=metadata,
        )
    )
