from pathlib import Path

import pytest
from rich.console import Console

from crewplane.adapters.artifacts.terminal_history import (
    FilesystemTerminalHistoryReader,
)
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.models import MemorySnapshot
from crewplane.core.memory.query import MAX_QUERY_CHARS
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import WorkflowPlan
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.project_memory import (
    accepted_memory,
    compile_memory_workflow,
    memory_config,
    memory_workflow,
)
from tests.helpers.terminal_results import RESULT_SOURCE_TOKEN, write_result_source
from tests.helpers.workspace_preflight import init_git_repo


@pytest.mark.parametrize("source_location", ["absent", "project", "external"])
def test_query_contributions_preserve_order_sources_and_literal_whitespace(
    tmp_path: Path, source_location: str
) -> None:
    task = memory_workflow("execute", "review")
    task.nodes[0].prompt_segments = [
        PromptSegment(
            role=PromptSegmentRole.SHARED,
            content="Start\n{{var:goal}}{{env:TOKEN}}tail\t",
        ),
        PromptSegment(role=PromptSegmentRole.REVIEWER, content="Review{{var:goal}}."),
        PromptSegment(role=PromptSegmentRole.EXECUTOR, content="Plain\ttext\n"),
        PromptSegment(role=PromptSegmentRole.SHARED, content="{{var:goal}}"),
    ]
    source_paths = {
        "absent": {},
        "project": {"work": tmp_path / "workflows/task.md"},
        "external": {"work": tmp_path.parent / "external/task.md"},
    }
    source = PreflightWorkflowSource.from_workflow(
        task, node_source_paths=source_paths[source_location]
    )

    preview = compile_memory_workflow(
        tmp_path, source, MemorySnapshot(), variables={"goal": "chosen-goal"}
    )

    assert not preview.has_errors(), preview.diagnostics
    assert [selection.role for selection in preview.memory_selections] == [
        "executor",
        "reviewer",
    ]
    for selection in preview.memory_selections:
        role = selection.role
        expected = [("identifier", "node", "work")]
        if source_location != "absent":
            identifier = (
                "workflows/task.md"
                if source_location == "project"
                else (tmp_path.parent / "external/task.md").as_posix()
            )
            expected.append(("identifier", "workflow", identifier))
        role_index, role_literal = (
            (2, "Plain\ttext\n") if role == "executor" else (1, "Review .")
        )
        expected.extend(
            [
                ("literal", f"work:{role}:0", "Start\n  tail\t"),
                ("literal", f"work:{role}:{role_index}", role_literal),
                ("literal", f"work:{role}:3", " "),
                ("variable", f"work:{role}:0:0", "chosen-goal"),
            ]
        )
        if role == "reviewer":
            expected.append(("variable", "work:reviewer:1:0", "chosen-goal"))
        expected.append(("variable", f"work:{role}:3:0", "chosen-goal"))
        assert [
            (item.kind, item.source, item.text)
            for item in selection.query.contributions
        ] == expected
        assert [(item.source, item.reason) for item in selection.query.exclusions] == [
            (f"work:{role}:0:1", "environment values are excluded")
        ]


def test_query_limit_records_omitted_captured_unicode_file_text(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_bytes(("界" * (MAX_QUERY_CHARS + 1)).encode())
    preview = compile_memory_workflow(
        tmp_path, memory_workflow("{{file:large.txt}}"), MemorySnapshot()
    )
    assert not preview.has_errors(), preview.diagnostics
    query = preview.memory_selections[0].query
    assert query.examined_chars == MAX_QUERY_CHARS
    assert len("".join(item.text for item in query.contributions)) == MAX_QUERY_CHARS
    assert any("character limit" in item.reason for item in query.exclusions)
    assert "�" not in str(query.contributions)


@requires_workspace_support
def test_project_initial_workspace_and_input_bytes_are_frozen_but_candidate_files_are_not(
    tmp_path: Path,
) -> None:
    (tmp_path / "context.md").write_bytes(b"captured-project-file")
    (tmp_path / "request.md").write_bytes(b"captured-request-file")
    source_snapshot = init_git_repo(tmp_path)
    (tmp_path / "context.md").write_bytes(b"changed-working-copy")
    task = memory_workflow(
        "{{file:context.md}} {{request.output}}",
        "{{file:context.md}} {{request.output}}",
    )
    task.worktrees = WorkflowPlan.model_validate(
        {
            "name": "Workspace",
            "nodes": [],
            "worktrees": {"primary": {"kind": "worktree"}},
        }
    ).worktrees
    task.nodes[0].needs = ["request"]
    task.nodes.insert(
        0,
        WorkflowPlan.model_validate(
            {
                "name": "Input",
                "nodes": [
                    {"id": "request", "mode": "input", "source": "{{file:request.md}}"}
                ],
            }
        ).nodes[0],
    )
    config = memory_config()
    config.settings.workspace.enabled = True
    runtime = build_runtime_config_snapshot(config, Console(), no_live=True).snapshot
    preview = compile_preflight_preview(
        PreflightWorkflowSource.from_workflow(task),
        config,
        runtime,
        PreflightCompileOptions(
            tmp_path,
            tmp_path / ".crewplane",
            memory_snapshot=MemorySnapshot(),
            workspace_source_snapshot=source_snapshot,
        ),
    )
    assert not preview.has_errors(), preview.diagnostics
    executor, reviewer = preview.memory_selections
    assert {item.text for item in executor.query.contributions} >= {
        "captured-project-file",
        "captured-request-file",
    }
    assert "captured-project-file" not in str(reviewer.query.contributions)
    assert "captured-request-file" in str(reviewer.query.contributions)
    assert "changed-working-copy" not in str(preview.memory_selections)
    assert any("runtime workspace" in item.reason for item in reviewer.query.exclusions)
    input_node = preview.nodes[0]
    assert input_node.input_workspace_file_locator_id is not None
    assert input_node.input_content_ref is None


def test_retained_provider_outputs_never_supply_query_terms_through_file_or_input(
    tmp_path: Path,
) -> None:
    write_result_source(tmp_path, content=b"provider-generated-keyword")
    task = memory_workflow(RESULT_SOURCE_TOKEN + " {{prior.output}}")
    task.nodes[0].needs = ["prior"]
    task.nodes.insert(
        0,
        WorkflowPlan.model_validate(
            {
                "name": "Input",
                "nodes": [
                    {"id": "prior", "mode": "input", "source": RESULT_SOURCE_TOKEN}
                ],
            }
        ).nodes[0],
    )
    config = memory_config()
    runtime = build_runtime_config_snapshot(config, Console(), no_live=True).snapshot
    preview = compile_preflight_preview(
        PreflightWorkflowSource.from_workflow(task),
        config,
        runtime,
        PreflightCompileOptions(
            tmp_path,
            tmp_path / ".crewplane",
            memory_snapshot=accepted_memory(
                LessonProposal(title="provider-generated-keyword", body="lesson")
            ),
            terminal_history_reader=FilesystemTerminalHistoryReader(
                tmp_path / ".crewplane"
            ),
        ),
    )
    assert not preview.has_errors(), preview.diagnostics
    selection = preview.memory_selections[0]
    assert selection.selected == ()
    assert "provider-generated-keyword" not in str(selection.query)
    assert len(selection.query.exclusions) == 2
    assert all(
        "retained provider-generated" in item.reason
        for item in selection.query.exclusions
    )
