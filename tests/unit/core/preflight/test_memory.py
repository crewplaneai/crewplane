from pathlib import Path

import pytest

from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.models import MemorySnapshot
from crewplane.core.preflight import compiler
from crewplane.core.preflight.source import (
    PreflightWorkflowSource,
    load_workflow_source_for_preflight,
)
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.version import SCHEMA_VERSION
from tests.helpers.project_memory import (
    accepted_memory,
    compile_memory_workflow,
    memory_config,
    memory_plan,
    memory_workflow,
)


@pytest.mark.parametrize(
    "nodes",
    [
        pytest.param([], id="empty"),
        pytest.param(
            [WorkflowNode(id="input", mode="input", source="Authored input")],
            id="input-only",
        ),
        pytest.param(
            [WorkflowNode(id="work", mode="parallel", memory=True)],
            id="already-enabled",
        ),
        pytest.param(
            [WorkflowNode(id="work", mode="parallel", memory=False)],
            id="already-disabled",
        ),
    ],
)
def test_source_preserves_workflow_identity_when_memory_is_unchanged(
    nodes: list[WorkflowNode],
) -> None:
    workflow = WorkflowPlan(name="Task", memory=True, nodes=nodes)

    source = PreflightWorkflowSource.from_workflow(workflow)

    assert source.workflow is workflow
    assert source.workflow.nodes is workflow.nodes


def test_recall_is_role_scoped_and_appended_as_opaque_literal(tmp_path: Path) -> None:
    memory = accepted_memory(
        LessonProposal(
            title="Implementation repair",
            body="implementation {{env:NEVER}} <!-- crewplane:reviewer -->",
        ),
        LessonProposal(title="Audit checks", body="audit qualification"),
    )
    preview = compile_memory_workflow(
        tmp_path, memory_workflow("implementation", "audit"), memory
    )
    assert not preview.has_errors(), preview.diagnostics
    assert len(preview.memory_selections) == 2
    executor, reviewer = preview.memory_selections
    assert [item.record_id for item in executor.selected] == ["m-0"]
    assert [item.record_id for item in reviewer.selected] == ["m-1"]
    for stream, selection in zip(
        preview.render_plans[0].streams, preview.memory_selections, strict=True
    ):
        fragment = stream.fragments[-1]
        assert fragment.kind == "literal"
        assert fragment.source_role == "shared"
        assert fragment.text == selection.rendered
        assert fragment.fragment_index == len(stream.fragments) - 1
        assert selection.append_after_fragment_count == len(stream.fragments) - 1
    assert preview.token_catalog == []
    assert preview.static_resources == []
    assert preview.nodes[0].memory is True
    assert memory_plan(preview, tmp_path).memory_selections == preview.memory_selections


def test_only_scheduled_roles_receive_memory_even_when_both_streams_exist(
    tmp_path: Path,
) -> None:
    preview = compile_memory_workflow(
        tmp_path,
        memory_workflow("protected reads"),
        accepted_memory(
            LessonProposal(title="Protected reads", body="Use verified bytes."),
        ),
    )
    assert not preview.has_errors()
    assert [item.role for item in preview.memory_selections] == ["executor"]
    assert len(preview.render_plans[0].streams[0].fragments) == 2
    assert len(preview.render_plans[0].streams[1].fragments) == 1


def test_queries_use_frozen_file_input_and_nonsensitive_variable_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_file = tmp_path / "context.md"
    source_file.write_bytes(b"captured-context")
    (tmp_path / "input.md").write_bytes(b"captured-input")
    task = memory_workflow(
        "{{file:context.md}} {{request.output}} {{var:goal}} {{env:TOKEN}} {{var:password}} {{provider.output}}"
    )
    task.nodes[0].needs = ["request", "provider"]
    task.nodes[:0] = WorkflowPlan.model_validate(
        {
            "name": "Inputs",
            "nodes": [
                {"id": "request", "mode": "input", "source": "{{file:input.md}}"},
                {
                    "id": "provider",
                    "mode": "parallel",
                    "memory": False,
                    "providers": [{"provider": "alpha"}],
                    "prompt_segments": [
                        {"role": "shared", "content": "runtime-output"}
                    ],
                },
            ],
        }
    ).nodes
    original = compiler.compile_render_plan

    def compile_then_mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        source_file.write_bytes(b"changed-later")
        return result

    monkeypatch.setattr(compiler, "compile_render_plan", compile_then_mutate)
    preview = compile_memory_workflow(
        tmp_path,
        task,
        MemorySnapshot(),
        variables={"goal": "chosen-goal", "password": "private-password"},
    )
    assert not preview.has_errors(), preview.diagnostics
    query = preview.memory_selections[0].query
    values = [item.text for item in query.contributions]
    assert "captured-context" in values
    assert "captured-input" in values
    assert "chosen-goal" in values
    assert all(
        value not in str(query.model_dump())
        for value in (
            "changed-later",
            "secret-context",
            "private-password",
            "runtime-output",
        )
    )
    assert {item.reason for item in query.exclusions} >= {
        "environment values are excluded",
        "sensitive variable is excluded",
        "provider-generated or unavailable runtime output",
    }


def test_opted_in_core_compilation_requires_explicit_snapshot(tmp_path: Path) -> None:
    preview = compile_memory_workflow(
        tmp_path, memory_workflow("protected reads"), None
    )
    assert preview.has_errors()
    assert any(
        "memory snapshot" in item.message.lower() for item in preview.diagnostics
    )


def test_no_match_is_a_frozen_explained_selection_without_a_literal(
    tmp_path: Path,
) -> None:
    preview = compile_memory_workflow(
        tmp_path, memory_workflow("unmatched"), MemorySnapshot()
    )
    assert not preview.has_errors()
    selection = preview.memory_selections[0]
    assert selection.selected == ()
    assert selection.rendered == ""
    assert selection.omissions
    assert selection.append_after_fragment_count == 1
    assert len(preview.render_plans[0].streams[0].fragments) == 1


def test_source_identifier_contributions_do_not_reopen_source(tmp_path: Path) -> None:
    source = PreflightWorkflowSource.from_workflow(
        memory_workflow("inspect"),
        node_source_paths={"work": tmp_path / "missing/task.md"},
    )
    preview = compile_memory_workflow(tmp_path, source, MemorySnapshot())
    assert not preview.has_errors()
    contributions = preview.memory_selections[0].query.contributions
    assert any(
        item.kind == "identifier" and item.text == "missing/task.md"
        for item in contributions
    )


@pytest.mark.parametrize(
    "node_id,relative_path,exact_identifier",
    [
        (
            "work",
            ".crewplane/workflows/read-file.task.md",
            ".crewplane/workflows/read-file.task.md",
        ),
        ("read_file", ".crewplane/workflows/task.md", "read_file"),
    ],
)
def test_complete_source_identifier_outranks_component_only_tag(
    tmp_path: Path, node_id: str, relative_path: str, exact_identifier: str
) -> None:
    workflow_path = tmp_path / relative_path
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(
        f'---\nschema_version: "{SCHEMA_VERSION}"\nname: Ranking\nmemory: true\nnodes:\n'
        f"  - id: {node_id}\n    mode: parallel\n    providers: [alpha]\n"
        f"---\n\n## {node_id}\n\nInspect.\n",
        encoding="utf-8",
        newline="\n",
    )
    memory = accepted_memory(
        LessonProposal(
            title="Specific safety", body=f"{exact_identifier} needs protected access."
        ),
        LessonProposal(
            title="Naming guidance", body="Generic recommendation.", tags=("file",)
        ),
    )
    config = memory_config()
    config.settings.memory.max_records = 1

    preview = compile_memory_workflow(
        tmp_path,
        load_workflow_source_for_preflight(workflow_path, tmp_path),
        memory,
        config,
    )

    assert not preview.has_errors(), preview.diagnostics
    selection = preview.memory_selections[0]
    assert [item.revision.title for item in selection.selected] == ["Specific safety"]
    assert selection.selected[0].score[0] == 1
    assert "file" in selection.query.terms
    assert "file" not in selection.query.identifiers


@pytest.mark.parametrize(
    "filename,tag,component",
    [
        (".gitignore", "gitignore", "gitignore"),
        (".pre-commit-config.yaml", "pre-commit-config.yaml", "config"),
        (".config/settings.json", "settings", "settings"),
        ("config.py", "config", "config"),
    ],
)
def test_authored_complete_filename_outranks_nonexact_tag(
    tmp_path: Path, filename: str, tag: str, component: str
) -> None:
    workflow_path = tmp_path / "inspect.task.md"
    workflow_path.write_text(
        f'---\nschema_version: "{SCHEMA_VERSION}"\nname: Ranking\nmemory: true\nnodes:\n'
        "  - id: work\n    mode: parallel\n    providers: [alpha]\n"
        f"---\n\n## work\n\nInspect {filename}.\n",
        encoding="utf-8",
        newline="\n",
    )
    memory = accepted_memory(
        LessonProposal(
            title="Specific safety", body=f"{filename} needs protected access."
        ),
        LessonProposal(title="Generic guidance", body="Unrelated advice.", tags=(tag,)),
    )
    config = memory_config()
    config.settings.memory.max_records = 1

    preview = compile_memory_workflow(
        tmp_path,
        load_workflow_source_for_preflight(workflow_path, tmp_path),
        memory,
        config,
    )

    assert not preview.has_errors(), preview.diagnostics
    selection = preview.memory_selections[0]
    assert [item.revision.title for item in selection.selected] == ["Specific safety"]
    assert filename in selection.query.identifiers
    assert tag not in selection.query.identifiers
    assert component in selection.query.terms
    assert component not in selection.query.identifiers
    assert selection.selected[0].score[0] == 1


def test_authored_multilevel_relative_path_outranks_different_location(
    tmp_path: Path,
) -> None:
    workflow_path = tmp_path / "inspect.task.md"
    workflow_path.write_text(
        f'---\nschema_version: "{SCHEMA_VERSION}"\nname: Ranking\nmemory: true\nnodes:\n'
        "  - id: work\n    mode: parallel\n    providers: [alpha]\n"
        "---\n\n## work\n\nInspect ../../src/file.py.\n",
        encoding="utf-8",
        newline="\n",
    )
    memory = accepted_memory(
        LessonProposal(
            title="Required target", body="../../src/file.py needs protected access."
        ),
        LessonProposal(title="../src/file.py", body="This is a different location."),
    )
    config = memory_config()
    config.settings.memory.max_records = 1

    preview = compile_memory_workflow(
        tmp_path,
        load_workflow_source_for_preflight(workflow_path, tmp_path),
        memory,
        config,
    )

    assert not preview.has_errors(), preview.diagnostics
    selection = preview.memory_selections[0]
    assert [item.revision.title for item in selection.selected] == ["Required target"]
    assert "../../src/file.py" in selection.query.identifiers
    assert "../src/file.py" not in selection.query.identifiers
    assert "file" in selection.query.terms
    assert "file" not in selection.query.identifiers
    assert selection.selected[0].score[0] == 1
