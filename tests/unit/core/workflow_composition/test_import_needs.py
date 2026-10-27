from dataclasses import replace
from pathlib import Path
from textwrap import dedent

import pytest

from crewplane.core.workflow.composition.imports import attach_import_prerequisites
from crewplane.core.workflow.composition.models import (
    ComposedNode,
    CompositionResult,
    ImportSpec,
)
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.models import WorkflowNode
from crewplane.core.workflow.validation import validate_workflow_plan
from crewplane.core.workspace.policy import WorktreeDeclaration


def _write(path: Path, frontmatter: str, body: str = "") -> Path:
    path.write_text(
        f"---\nname: Example\n{dedent(frontmatter).strip()}\n---\n{dedent(body)}",
        encoding="utf-8",
    )
    return path


def _load(path: Path) -> dict[str, WorkflowNode]:
    workflow = load_tasks_with_sources(path, project_root=path.parent).workflow
    return {node.id: node for node in validate_workflow_plan(workflow).nodes}


@pytest.fixture
def child(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "child.task.md",
        """
        nodes:
          - id: review
            mode: sequential
            depth: 1
            providers: [{provider: mock}, {provider: mock, role: reviewer}]
          - {id: summarize, mode: sequential, providers: [mock], needs: [review]}
        """,
        """
        ## review
        Review the documentation.
        <!-- crewplane:reviewer -->
        Check accuracy.
        <!-- /crewplane:reviewer -->
        ## summarize
        Summarize {{review.output}}.
        """,
    )


def test_import_needs_gates_unchanged_child_after_plan_and_implementation(
    tmp_path: Path, child: Path
) -> None:
    standalone = _load(child)
    _write(
        tmp_path / "plan.task.md",
        "nodes: [{id: plan.final, mode: sequential, providers: [mock]}]",
        "## plan.final\nWrite the plan.",
    )
    _write(
        tmp_path / "implement.task.md",
        """
        inputs: {implementation_plan: plan-input}
        nodes:
          - {id: plan-input, mode: input, source: '{{file:plan.md}}'}
          - {id: implement.handoff, mode: sequential, providers: [mock], needs: [plan-input]}
        """,
        "## implement.handoff\nImplement {{plan-input.output}}.",
    )
    root = _write(
        tmp_path / "root.task.md",
        """
        imports:
          - {path: plan.task.md, as: plan}
          - path: implement.task.md
            as: implement
            inputs: {implementation_plan: plan.plan.final}
          - path: child.task.md
            as: doc
            needs: [plan.plan.final, implement.implement.handoff]
        nodes: []
        """,
    )

    nodes = _load(root)

    assert nodes["doc.review"].needs == [
        "plan.plan.final",
        "implement.implement.handoff",
    ]
    assert nodes["doc.summarize"].needs == ["doc.review"]
    assert nodes["implement.implement.handoff"].needs == ["plan.plan.final"]
    assert nodes["doc.review"].prompt_segments == standalone["review"].prompt_segments
    assert _load(child) == standalone
    source = load_tasks_with_sources(root, project_root=tmp_path)
    child_source = load_tasks_with_sources(child, project_root=tmp_path)
    assert source.node_source_paths["doc.review"] == child
    assert (
        source.node_source_spans["doc.review"]
        == child_source.node_source_spans["review"]
    )
    assert (
        source.prompt_segment_spans["doc.review"]
        == child_source.prompt_segment_spans["review"]
    )


def test_import_needs_gates_all_entries_after_binding_and_preserves_internal_edges(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "child.task.md",
        """
        inputs: {context: bound-input, standards: file-input}
        nodes:
          - {id: bound-input, mode: input, source: '{{file:context.md}}'}
          - {id: file-input, mode: input, source: '{{file:standards.md}}'}
          - {id: bound-branch, mode: sequential, providers: [mock], needs: [bound-input]}
          - {id: file-branch, mode: sequential, providers: [mock], needs: [file-input]}
          - {id: independent, mode: sequential, providers: [mock]}
          - {id: finish, mode: sequential, providers: [mock], needs: [bound-branch, file-branch, independent]}
        """,
        """
        ## bound-branch
        Read {{bound-input.output}}.
        ## file-branch
        Read {{file-input.output}}.
        ## independent
        Check independently.
        ## finish
        Done.
        """,
    )
    root = _write(
        tmp_path / "root.task.md",
        """
        imports:
          - path: child.task.md
            as: child
            inputs: {context: caller}
            needs: [gate, caller, gate]
        nodes:
          - {id: caller, mode: sequential, providers: [mock]}
          - {id: gate, mode: sequential, providers: [mock], needs: [caller]}
        """,
        "## caller\nContext.\n## gate\nPrerequisite.",
    )

    nodes = _load(root)

    assert "child.bound-input" not in nodes
    assert nodes["child.file-input"].needs == []
    assert nodes["child.bound-branch"].needs == ["caller", "gate"]
    assert nodes["child.file-branch"].needs == ["child.file-input", "gate", "caller"]
    assert nodes["child.independent"].needs == ["gate", "caller"]
    assert nodes["child.finish"].needs == [
        "child.bound-branch",
        "child.file-branch",
        "child.independent",
    ]


@pytest.mark.usefixtures("child")
def test_nested_import_needs_resolves_bound_caller_node_ids_and_later_siblings(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "wrapper.task.md",
        """
        inputs: {public_context: local-input}
        imports:
          - path: child.task.md
            as: nested.child
            needs: [local-input, later.step.review]
          - {path: child.task.md, as: later.step}
        nodes:
          - {id: local-input, mode: input, source: '{{file:context.md}}'}
          - {id: local.branch, mode: sequential, providers: [mock], needs: [local-input]}
        """,
        "## local.branch\nIndependent branch.",
    )
    root = _write(
        tmp_path / "root.task.md",
        """
        imports:
          - path: wrapper.task.md
            as: bundle.outer
            inputs: {public_context: caller}
            needs: [outer-gate]
        nodes:
          - {id: caller, mode: sequential, providers: [mock]}
          - {id: outer-gate, mode: sequential, providers: [mock]}
        """,
        "## caller\nContext.\n## outer-gate\nGate.",
    )

    nodes = _load(root)

    assert nodes["bundle.outer.nested.child.review"].needs == [
        "caller",
        "bundle.outer.later.step.review",
    ]
    assert nodes["bundle.outer.later.step.review"].needs == ["outer-gate"]
    assert nodes["bundle.outer.local.branch"].needs == ["caller", "outer-gate"]
    assert nodes["bundle.outer.nested.child.summarize"].needs == [
        "bundle.outer.nested.child.review"
    ]
    assert "bundle.outer.local-input" not in nodes


@pytest.mark.usefixtures("child")
def test_dotted_sibling_ids_are_not_mistaken_for_child_members(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "wrapper.task.md",
        """
        inputs: {context: local-input}
        nodes:
          - {id: local-input, mode: input, source: '{{file:context.md}}'}
          - {id: entry, mode: sequential, providers: [mock], needs: [local-input]}
        """,
        "## entry\nRead context.",
    )
    root = _write(
        tmp_path / "root.task.md",
        """
        imports:
          - path: wrapper.task.md
            as: child
            inputs: {context: child.sibling.review}
            needs: [gate]
          - {path: child.task.md, as: child.sibling}
        nodes: [{id: gate, mode: sequential, providers: [mock]}]
        """,
        "## gate\nGate.",
    )

    nodes = _load(root)

    assert nodes["child.entry"].needs == ["child.sibling.review", "gate"]


def test_repeated_imports_do_not_leak_prerequisites_into_cached_definition(
    tmp_path: Path, child: Path
) -> None:
    original = child.read_bytes()
    root = _write(
        tmp_path / "root.task.md",
        """
        imports:
          - {path: child.task.md, as: first, needs: [one]}
          - {path: child.task.md, as: second, needs: [two]}
          - {path: child.task.md, as: third}
          - {path: child.task.md, as: fourth, needs: []}
        nodes:
          - {id: one, mode: sequential, providers: [mock]}
          - {id: two, mode: sequential, providers: [mock]}
        """,
        "## one\nOne.\n## two\nTwo.",
    )

    nodes = _load(root)

    assert [
        nodes[f"{alias}.review"].needs
        for alias in ("first", "second", "third", "fourth")
    ] == [["one"], ["two"], [], []]
    assert child.read_bytes() == original
    assert _load(child)["review"].needs == []


@pytest.mark.parametrize(
    ("needs", "gate_needs", "message"),
    [
        ("[missing]", "[]", "depends on unknown node 'missing'"),
        ("[child]", "[]", "depends on unknown node 'child'"),
        ("[child.review]", "[]", "cannot depend on itself"),
        ("[child.summarize]", "[]", "contains a cycle"),
        ("[gate]", "[child.summarize]", "contains a cycle"),
    ],
)
@pytest.mark.usefixtures("child")
def test_import_needs_uses_final_graph_validation(
    tmp_path: Path, needs: str, gate_needs: str, message: str
) -> None:
    root = _write(
        tmp_path / "root.task.md",
        f"""
        imports:
          - {{path: child.task.md, as: child, needs: {needs}}}
        nodes:
          - {{id: gate, mode: sequential, providers: [mock], needs: {gate_needs}}}
        """,
        "## gate\nGate.",
    )

    with pytest.raises(ValueError, match=message):
        _load(root)


@pytest.mark.parametrize("bound", [False, True])
def test_nonempty_import_needs_rejects_input_only_or_pruned_child(
    tmp_path: Path, bound: bool
) -> None:
    _write(
        tmp_path / "child.task.md",
        """
        inputs: {context: file-input}
        nodes: [{id: file-input, mode: input, source: '{{file:context.md}}'}]
        """,
    )
    binding = "inputs: {context: gate}" if bound else ""
    root = _write(
        tmp_path / "root.task.md",
        f"""
        imports:
          - path: child.task.md
            as: child
            needs: [gate]
            {binding}
        nodes: [{{id: gate, mode: sequential, providers: [mock]}}]
        """,
        "## gate\nGate.",
    )

    with pytest.raises(ValueError, match="no provider entry nodes") as caught:
        _load(root)

    assert str(root) in str(caught.value)
    assert "'child'" in str(caught.value)


def test_import_needs_rejects_reference_to_pruned_input(tmp_path: Path) -> None:
    _write(
        tmp_path / "child.task.md",
        """
        inputs: {context: file-input}
        nodes:
          - {id: file-input, mode: input, source: '{{file:context.md}}'}
          - {id: entry, mode: sequential, providers: [mock], needs: [file-input]}
        """,
        "## entry\nReview.",
    )
    root = _write(
        tmp_path / "root.task.md",
        """
        imports:
          - path: child.task.md
            as: child
            inputs: {context: gate}
            needs: [child.file-input]
        nodes: [{id: gate, mode: sequential, providers: [mock]}]
        """,
        "## gate\nGate.",
    )

    with pytest.raises(ValueError, match="depends on unknown node 'child.file-input'"):
        _load(root)


def test_attaching_prerequisites_preserves_metadata_and_source_dependencies(
    tmp_path: Path,
) -> None:
    node = ComposedNode(
        payload=WorkflowNode(id="child.entry", mode="sequential", needs=["caller"]),
        source_path=tmp_path / "child.task.md",
        source_span={"start_line": 10, "end_line": 12},
        prompt_segment_spans=({"start_line": 11, "end_line": 12},),
        local_worktree_count=1,
        implicit_worktree_selector="child.shared",
    )
    source = CompositionResult(
        nodes=[node],
        worktrees={"child.shared": WorktreeDeclaration(kind="worktree")},
        consumed_param_bindings={"child:topic"},
    )
    spec = ImportSpec(
        "child", "child.task.md", {}, {}, tmp_path / "root.task.md", ("gate",)
    )

    result = attach_import_prerequisites(source, ("gate",), spec)

    assert result.nodes[0].payload.needs == ["caller", "gate"]
    assert result.nodes[0] == replace(node, payload=result.nodes[0].payload)
    assert result.worktrees == source.worktrees
    assert result.consumed_param_bindings == {"child:topic"}
    assert node.payload.needs == ["caller"]
    assert result is not source
    assert result.nodes[0] is not node
