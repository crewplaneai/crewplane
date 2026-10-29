"""Traverse Markdown imports and foreach templates while collecting source metadata."""

from __future__ import annotations

import hashlib
from pathlib import Path

from crewplane.core.workspace.policy import WorktreeDeclaration

from ..markdown import parse_workflow_markdown_document
from ..models import WorkflowNode
from ..source_locations import SourceSpan
from .foreach import resolve_foreach_inputs
from .imports import (
    attach_import_prerequisites,
    bind_import_params,
    resolve_import_path,
    validate_import_params_consumed,
)
from .materialization import (
    materialize_foreach_template,
    materialize_workflow_payload,
)
from .models import (
    ComposedForeach,
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
    """Compose one workflow graph within a resolved project root.

    Use a fresh instance for each independent composition. Parsed files, hashes,
    discovery order, schema, node collisions, and templates persist across calls,
    including failed calls; failures do not roll back collected state. Traversal
    is synchronous, and instances are not safe for concurrent or reentrant use.
    Files are read without newline translation and are never written.
    """

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()
        self._parsed_cache: dict[Path, ParsedWorkflow] = {}
        self._source_hashes: dict[Path, str] = {}
        self._source_order: list[Path] = []
        self._node_sources: dict[str, Path] = {}
        self._node_source_spans: dict[str, SourceSpan] = {}
        self._prompt_segment_spans: dict[str, list[SourceSpan]] = {}
        self._root_schema_version: str | None = None
        self._foreach_templates: dict[str, ComposedForeach] = {}
        self._active_foreach: str | None = None

    def compose(self, path: Path) -> ComposedWorkflowDocument:
        """Return a composed payload and collected source metadata.

        Resolve the root against the working directory; imported and foreach
        files must stay within the project root. Visit imports depth first in
        declaration order before local nodes, and record sources on first read.
        Root metadata and repetition settings govern the returned payload.

        Workflow validation errors raise ValueError. Malformed YAML can propagate
        yaml.YAMLError subclasses, including ParserError and ScannerError.
        Filesystem errors also propagate. State collected before failure remains,
        but the active foreach guard is cleared when child traversal exits.
        Returned metadata mappings are shallow copies.
        """
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
        return self._build_document(root_workflow, composition)

    def _build_document(
        self, root_workflow: ParsedWorkflow, composition: CompositionResult
    ) -> ComposedWorkflowDocument:
        workflow_payload = materialize_workflow_payload(root_workflow, composition)
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
            foreach_templates=dict(self._foreach_templates),
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

        context = self._build_context(
            path,
            namespace_prefix,
            inherited_params,
            bound_inputs,
            inherited_implicit_worktree_selector,
            import_stack,
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

    def _build_context(
        self,
        path: Path,
        namespace_prefix: str,
        inherited_params: dict[str, ParamBinding],
        bound_inputs: dict[str, str],
        inherited_implicit_worktree_selector: str | None,
        import_stack: tuple[Path, ...],
    ) -> CompositionContext:
        workflow = self._load_workflow_file(path)
        self._set_or_validate_root_schema(path, workflow.schema_version)
        return CompositionContext(
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
        prerequisites = _resolve_import_prerequisites(context, import_spec)
        child_result = self._compose_file(
            path=resolved_import_path,
            namespace_prefix=child_namespace,
            inherited_params=bind_import_params(
                context.inherited_params,
                child_namespace,
                import_spec.with_params,
            ),
            bound_inputs=_resolve_import_inputs(context, import_spec),
            inherited_implicit_worktree_selector=context.implicit_worktree_selector,
            import_stack=context.import_stack,
        )
        validate_import_params_consumed(import_spec, child_namespace, child_result)
        return attach_import_prerequisites(child_result, prerequisites, import_spec)

    def compose_local_nodes(self, context: CompositionContext) -> CompositionResult:
        """Compose this context's unbound nodes in declaration order.

        The caller handles this context's imports and schema validation. Register
        node IDs and copied source spans on this composer, and compile foreach
        children immediately after their group. Bound input fallbacks are omitted.
        Return local worktrees and consumed parameter binding IDs; foreach template
        nodes remain separate from the returned nodes. Validation errors propagate
        without rolling back registrations or completed templates.
        """
        composed_nodes: list[ComposedNode] = []
        consumed_param_bindings: set[str] = set()

        for node in context.workflow.nodes:
            if node.payload.id in context.bound_input_nodes:
                continue

            composed_node, consumed_here = compose_local_node(context, node)
            consumed_param_bindings.update(consumed_here)
            self._append_node(composed_nodes, composed_node)
            if composed_node.payload.mode == "foreach":
                consumed_param_bindings.update(
                    self._compose_foreach(context, composed_node)
                )

        return CompositionResult(
            nodes=composed_nodes,
            worktrees=_compose_worktree_declarations(context),
            consumed_param_bindings=consumed_param_bindings,
        )

    def _compose_foreach(
        self, context: CompositionContext, group: ComposedNode
    ) -> set[str]:
        node = group.payload
        self._assert_no_active_foreach(node.id)
        if node.workflow is None or node.item_input is None:
            raise ValueError(f"Incomplete foreach '{node.id}'")
        spec = ImportSpec(
            node.id, node.workflow, node.with_params, node.inputs, group.source_path
        )
        path = self._resolve_import_file(spec)
        child = self._load_workflow_file(path)
        item_id, shared = resolve_foreach_inputs(
            child, node.item_input, node.inputs, node.id
        )
        namespace = f"{node.id}.template"
        params = bind_import_params(
            context.inherited_params, namespace, node.with_params
        )
        result = self._traverse_foreach_template(context, node, path, params)
        validate_import_params_consumed(spec, namespace, result)
        self._foreach_templates[node.id] = materialize_foreach_template(
            node.id, item_id, child, params, shared, result
        )
        return result.consumed_param_bindings

    def _assert_no_active_foreach(self, group_id: str) -> None:
        if self._active_foreach is not None:
            raise ValueError(
                f"Foreach '{self._active_foreach}' cannot contain nested foreach '{group_id}'"
            )

    def _traverse_foreach_template(
        self,
        context: CompositionContext,
        node: WorkflowNode,
        path: Path,
        params: dict[str, ParamBinding],
    ) -> CompositionResult:
        self._active_foreach = node.id
        try:
            return self._compose_file(
                path,
                f"{node.id}.template",
                params,
                node.inputs,
                None,
                context.import_stack,
            )
        finally:
            self._active_foreach = None

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
            self._node_source_spans[node_id] = node.source_span.copy()
        self._prompt_segment_spans[node_id] = [
            span.copy() for span in node.prompt_segment_spans
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


def _merge_worktrees(
    first: dict[str, WorktreeDeclaration],
    second: dict[str, WorktreeDeclaration],
) -> dict[str, WorktreeDeclaration]:
    collisions = sorted(set(first).intersection(second))
    if collisions:
        joined = ", ".join(collisions)
        raise ValueError(f"Worktree declaration collision after composition: {joined}")
    return {**first, **second}


def _resolve_import_prerequisites(
    context: CompositionContext, import_spec: ImportSpec
) -> tuple[str, ...]:
    return tuple(
        resolve_dependency_id(
            dependency, context.namespace_prefix, context.bound_input_nodes
        )
        for dependency in import_spec.needs
    )


def _resolve_import_inputs(
    context: CompositionContext, import_spec: ImportSpec
) -> dict[str, str]:
    return {
        key: resolve_dependency_id(
            source_id, context.namespace_prefix, context.bound_input_nodes
        )
        for key, source_id in import_spec.inputs.items()
    }
