from __future__ import annotations

import hashlib
from pathlib import Path

from ..workflow_markdown import parse_workflow_markdown_document
from ..workflow_models import WorkflowPayload, workflow_node_payload_dict
from ..workspace_policy import (
    PROJECT_ROOT_WORKTREE_SELECTOR,
    WorktreeDeclaration,
    worktree_declarations_payload,
)
from .imports import (
    bind_import_params,
    resolve_import_path,
    validate_import_params_consumed,
)
from .models import (
    ComposedNode,
    ComposedWorkflowDocument,
    CompositionContext,
    CompositionResult,
    ImportSpec,
    ParamBinding,
    ParsedWorkflow,
    WorkflowSourceRecord,
)
from .nodes import compose_local_node, resolve_bound_input_nodes
from .parsing import (
    parsed_workflow_from_markdown,
)
from .rewrites import qualify_id, resolve_dependency_id


class WorkflowComposer:
    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()
        self._parsed_cache: dict[Path, ParsedWorkflow] = {}
        self._source_hashes: dict[Path, str] = {}
        self._source_order: list[Path] = []
        self._node_sources: dict[str, Path] = {}
        self._node_source_spans: dict[str, dict[str, int]] = {}
        self._prompt_segment_spans: dict[str, list[dict[str, int]]] = {}
        self._root_schema_version: str | None = None

    def compose(self, path: Path) -> ComposedWorkflowDocument:
        root_path = path.resolve(strict=False)
        self._assert_file_exists(root_path, "Workflow file")

        root_workflow = self._load_workflow_file(root_path)
        self._set_or_validate_root_schema(root_path, root_workflow.schema_version)

        composition = self._compose_file(
            path=root_path,
            namespace_prefix="",
            inherited_params={},
            bound_inputs={},
            inherited_implicit_worktree_selector=None,
            import_stack=(),
        )
        composed_nodes = _materialize_implicit_worktree_selectors(
            composition.nodes,
            composed_worktree_count=len(composition.worktrees),
            root_worktree_count=len(root_workflow.worktrees),
        )

        workflow_payload: WorkflowPayload = {
            "schema_version": root_workflow.schema_version,
            "name": root_workflow.name,
            "description": root_workflow.description,
            "inputs": dict(root_workflow.inputs),
            "nodes": [
                workflow_node_payload_dict(node.payload) for node in composed_nodes
            ],
        }
        if composition.worktrees:
            workflow_payload["worktrees"] = worktree_declarations_payload(
                composition.worktrees
            )
        source_records = [
            WorkflowSourceRecord(
                path=source_path, sha256=self._source_hashes[source_path]
            )
            for source_path in self._source_order
        ]
        return ComposedWorkflowDocument(
            workflow_payload=workflow_payload,
            source_records=source_records,
            node_source_paths=dict(self._node_sources),
            node_source_spans=dict(self._node_source_spans),
            prompt_segment_spans=dict(self._prompt_segment_spans),
        )

    def _compose_file(
        self,
        path: Path,
        namespace_prefix: str,
        inherited_params: dict[str, ParamBinding],
        bound_inputs: dict[str, str],
        inherited_implicit_worktree_selector: str | None,
        import_stack: tuple[Path, ...],
    ) -> CompositionResult:
        if path in import_stack:
            chain = " -> ".join(str(segment) for segment in (*import_stack, path))
            raise ValueError(f"Workflow import cycle detected: {chain}")

        workflow = self._load_workflow_file(path)
        self._set_or_validate_root_schema(path, workflow.schema_version)
        context = CompositionContext(
            workflow=workflow,
            namespace_prefix=namespace_prefix,
            inherited_params=inherited_params,
            bound_input_nodes=resolve_bound_input_nodes(
                path,
                workflow.inputs,
                bound_inputs,
            ),
            import_stack=(*import_stack, path),
            implicit_worktree_selector=_context_implicit_worktree_selector(
                namespace_prefix,
                workflow.worktrees,
                inherited_implicit_worktree_selector,
            ),
        )

        import_result = self._compose_imports(context)
        local_result = self.compose_local_nodes(context)
        return CompositionResult(
            nodes=[*import_result.nodes, *local_result.nodes],
            worktrees=_merge_worktrees(import_result.worktrees, local_result.worktrees),
            consumed_param_bindings=(
                import_result.consumed_param_bindings
                | local_result.consumed_param_bindings
            ),
        )

    def _compose_imports(self, context: CompositionContext) -> CompositionResult:
        composed_nodes: list[ComposedNode] = []
        composed_worktrees: dict[str, WorktreeDeclaration] = {}
        consumed_param_bindings: set[str] = set()

        for import_spec in context.workflow.imports:
            child_result = self._compose_import(context, import_spec)
            consumed_param_bindings.update(child_result.consumed_param_bindings)
            composed_nodes.extend(child_result.nodes)
            composed_worktrees = _merge_worktrees(
                composed_worktrees,
                child_result.worktrees,
            )

        return CompositionResult(
            nodes=composed_nodes,
            worktrees=composed_worktrees,
            consumed_param_bindings=consumed_param_bindings,
        )

    def _compose_import(
        self,
        context: CompositionContext,
        import_spec: ImportSpec,
    ) -> CompositionResult:
        resolved_import_path = self._resolve_import_file(import_spec)
        child_namespace = qualify_id(context.namespace_prefix, import_spec.alias)
        child_result = self._compose_file(
            path=resolved_import_path,
            namespace_prefix=child_namespace,
            inherited_params=bind_import_params(
                context.inherited_params,
                child_namespace,
                import_spec.with_params,
            ),
            bound_inputs={
                key: resolve_dependency_id(
                    source_id,
                    namespace_prefix=context.namespace_prefix,
                    bound_input_nodes=context.bound_input_nodes,
                )
                for key, source_id in import_spec.inputs.items()
            },
            inherited_implicit_worktree_selector=context.implicit_worktree_selector,
            import_stack=context.import_stack,
        )
        validate_import_params_consumed(import_spec, child_namespace, child_result)
        return child_result

    def compose_local_nodes(self, context: CompositionContext) -> CompositionResult:
        composed_nodes: list[ComposedNode] = []
        consumed_param_bindings: set[str] = set()

        for node in context.workflow.nodes:
            if node.payload.id in context.bound_input_nodes:
                continue

            composed_node, consumed_here = compose_local_node(context, node)
            consumed_param_bindings.update(consumed_here)
            self._append_node(composed_nodes, composed_node)

        return CompositionResult(
            nodes=composed_nodes,
            worktrees=_compose_worktree_declarations(context),
            consumed_param_bindings=consumed_param_bindings,
        )

    def _resolve_import_file(self, import_spec: ImportSpec) -> Path:
        resolved_import_path = resolve_import_path(
            import_spec.source_path,
            import_spec.raw_path,
        )
        self._assert_project_path(
            resolved_import_path,
            label=(
                f"Imported workflow '{import_spec.raw_path}' referenced from "
                f"'{import_spec.source_path}'"
            ),
        )
        self._assert_file_exists(resolved_import_path, "Imported workflow")

        if resolved_import_path.suffix.lower() != ".md":
            raise ValueError(
                "Imported workflow must be a markdown file (.md): "
                f"{resolved_import_path}"
            )
        return resolved_import_path

    def _append_node(
        self,
        composed_nodes: list[ComposedNode],
        node: ComposedNode,
    ) -> None:
        node_id = node.payload.id
        existing_source = self._node_sources.get(node_id)
        if existing_source is not None:
            raise ValueError(
                "Node ID collision after workflow composition for "
                f"'{node_id}': '{existing_source}' and '{node.source_path}'."
            )
        self._node_sources[node_id] = node.source_path
        if node.source_span is not None:
            self._node_source_spans[node_id] = dict(node.source_span)
        self._prompt_segment_spans[node_id] = [
            dict(span) for span in node.prompt_segment_spans
        ]
        composed_nodes.append(node)

    def _load_workflow_file(self, path: Path) -> ParsedWorkflow:
        cached = self._parsed_cache.get(path)
        if cached is not None:
            return cached

        self._assert_file_exists(path, "Workflow file")
        with path.open("r", encoding="utf-8", newline="") as handle:
            raw_content = handle.read()
        if path not in self._source_hashes:
            self._source_hashes[path] = hashlib.sha256(
                raw_content.encode("utf-8")
            ).hexdigest()
            self._source_order.append(path)

        parsed_document = parse_workflow_markdown_document(path, raw_content)
        parsed = parsed_workflow_from_markdown(parsed_document, path)
        self._parsed_cache[path] = parsed
        return parsed

    def _set_or_validate_root_schema(
        self, source_path: Path, schema_version: str
    ) -> None:
        if self._root_schema_version is None:
            self._root_schema_version = schema_version
            return
        if schema_version == self._root_schema_version:
            return
        raise ValueError(
            f"Workflow '{source_path}' has schema_version '{schema_version}', "
            "but root workflow uses "
            f"'{self._root_schema_version}'."
        )

    def _assert_project_path(self, path: Path, label: str) -> None:
        if path.is_relative_to(self._project_root):
            return
        raise ValueError(
            f"{label} resolves outside project root '{self._project_root}': {path}"
        )

    @staticmethod
    def _assert_file_exists(path: Path, label: str) -> None:
        if not path.exists():
            raise ValueError(f"{label} does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"{label} is not a file: {path}")


def _compose_worktree_declarations(
    context: CompositionContext,
) -> dict[str, WorktreeDeclaration]:
    return {
        qualify_id(context.namespace_prefix, name): declaration
        for name, declaration in context.workflow.worktrees.items()
    }


def _context_implicit_worktree_selector(
    namespace_prefix: str,
    worktrees: dict[str, WorktreeDeclaration],
    inherited_selector: str | None,
) -> str | None:
    if len(worktrees) == 1:
        return qualify_id(namespace_prefix, next(iter(worktrees)))
    if not worktrees:
        return inherited_selector
    return None


def _materialize_implicit_worktree_selectors(
    nodes: list[ComposedNode],
    composed_worktree_count: int,
    root_worktree_count: int,
) -> list[ComposedNode]:
    if composed_worktree_count == 0:
        return nodes
    return [
        _materialize_worktree_selector(node)
        if _needs_materialized_selector(
            node,
            composed_worktree_count,
            root_worktree_count,
        )
        else node
        for node in nodes
    ]


def _needs_materialized_selector(
    node: ComposedNode,
    composed_worktree_count: int,
    root_worktree_count: int,
) -> bool:
    if node.payload.mode == "input":
        return False
    if node.payload.worktree is not None:
        return False
    if node.local_worktree_count == 0:
        if node.implicit_worktree_selector is not None:
            return composed_worktree_count > 1
        return root_worktree_count != 1 or composed_worktree_count > 1
    return node.implicit_worktree_selector is not None and composed_worktree_count > 1


def _materialize_worktree_selector(node: ComposedNode) -> ComposedNode:
    return ComposedNode(
        payload=node.payload.model_copy(
            update={
                "worktree": node.implicit_worktree_selector
                or PROJECT_ROOT_WORKTREE_SELECTOR
            }
        ),
        source_path=node.source_path,
        source_span=node.source_span,
        prompt_segment_spans=node.prompt_segment_spans,
        local_worktree_count=node.local_worktree_count,
        implicit_worktree_selector=node.implicit_worktree_selector,
    )


def _merge_worktrees(
    first: dict[str, WorktreeDeclaration],
    second: dict[str, WorktreeDeclaration],
) -> dict[str, WorktreeDeclaration]:
    collisions = sorted(set(first).intersection(second))
    if collisions:
        joined = ", ".join(collisions)
        raise ValueError(f"Worktree declaration collision after composition: {joined}")
    return {**first, **second}
