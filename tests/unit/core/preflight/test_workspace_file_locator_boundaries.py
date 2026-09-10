import subprocess
from pathlib import Path

import pytest

from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.core.workspace import (
    git_reads as workspace_git_file_reads,
)
from tests.helpers.workspace_preflight import (
    compile_workflow_with_source_snapshot,
    init_git_repo,
    workspace_source_snapshot,
    workspace_workflow,
)


def test_workspace_enabled_allowlisted_absolute_file_tokens_remain_static(
    tmp_path: Path,
) -> None:
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / "context.md"
    external_file.write_text("external context", encoding="utf-8")

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow(f"Read {{{{file:{external_file.as_posix()}}}}}"),
        workspace_source_snapshot("a" * 40),
        allowed_template_paths=(external_dir,),
    )

    assert preview.diagnostics == []
    assert preview.workflow_signature is not None
    assert [resource.raw_path for resource in preview.static_resources] == [
        external_file.as_posix()
    ]


def test_workspace_enabled_missing_project_initial_file_locator_fails(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow("Read {{file:docs/missing.md}}"),
        source_snapshot,
    )

    assert preview.workflow_signature is None
    assert [(item.code, item.phase, item.node_id) for item in preview.diagnostics] == [
        ("WORKSPACE-FILE-LOCATOR", "workspace_file_locator_policy", "implement")
    ]
    assert "does not resolve" in preview.diagnostics[0].message


def test_workspace_enabled_file_locator_rejects_preflight_runtime_root(
    tmp_path: Path,
) -> None:
    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow("Read {{file:.crewplane/preflight/fingerprint.key}}"),
        workspace_source_snapshot("a" * 40),
    )

    assert preview.workflow_signature is None
    assert [(item.code, item.phase, item.node_id) for item in preview.diagnostics] == [
        ("WORKSPACE-FILE-LOCATOR", "workspace_file_locator_policy", "implement")
    ]
    assert "reserved .crewplane runtime roots" in preview.diagnostics[0].message


def test_workspace_enabled_file_locator_allows_crewplane_inputs(
    tmp_path: Path,
) -> None:
    input_file = tmp_path / ".crewplane" / "inputs" / "context.md"
    input_file.parent.mkdir(parents=True)
    input_file.write_text("input context\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow("Read {{file:.crewplane/inputs/context.md}}"),
        source_snapshot,
    )

    assert preview.diagnostics == []
    locator = preview.workspace_file_locators[0]
    assert locator.workspace_relative_path == ".crewplane/inputs/context.md"
    assert locator.content_ref is not None
    assert preview.workspace_file_payloads == {locator.content_ref: b"input context\n"}


def test_workspace_enabled_unresolved_home_file_locator_reports_diagnostic(
    tmp_path: Path,
) -> None:
    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow(
            "Read {{file:~crewplane_missing_user_for_tests/context.md}}"
        ),
        workspace_source_snapshot("a" * 40),
    )

    assert preview.workflow_signature is None
    assert [(item.code, item.phase, item.node_id) for item in preview.diagnostics] == [
        ("WORKSPACE-FILE-LOCATOR", "workspace_file_locator_policy", "implement")
    ]
    assert "could not expand user home" in preview.diagnostics[0].message


def test_workspace_enabled_project_initial_file_lookup_timeout_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def timed_out_run(command: list[str], **kwargs: object) -> object:
        assert kwargs["timeout"] == (
            workspace_git_file_reads.WORKSPACE_GIT_FILE_READ_TIMEOUT_SECONDS
        )
        raise subprocess.TimeoutExpired(command, 0.01)

    monkeypatch.setattr(
        workspace_git_file_reads.subprocess,
        "run",
        timed_out_run,
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow("Read {{file:docs/requirements.md}}"),
        workspace_source_snapshot("a" * 40),
    )

    assert preview.workflow_signature is None
    assert [(item.code, item.phase, item.node_id) for item in preview.diagnostics] == [
        ("WORKSPACE-FILE-LOCATOR", "workspace_file_locator_policy", "implement")
    ]
    assert "Git command timed out" in preview.diagnostics[0].message


def test_workspace_enabled_project_initial_blob_timeout_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def run_git(command: list[str], **kwargs: object) -> object:
        assert kwargs["timeout"] == (
            workspace_git_file_reads.WORKSPACE_GIT_FILE_READ_TIMEOUT_SECONDS
        )
        if "ls-tree" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    b"100644 blob aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    b"\tdocs/requirements.md\0"
                ),
                stderr=b"",
            )
        raise subprocess.TimeoutExpired(command, 0.01)

    monkeypatch.setattr(
        workspace_git_file_reads.subprocess,
        "run",
        run_git,
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow("Read {{file:docs/requirements.md}}"),
        workspace_source_snapshot("a" * 40),
    )

    assert preview.workflow_signature is None
    assert [(item.code, item.phase, item.node_id) for item in preview.diagnostics] == [
        ("WORKSPACE-FILE-LOCATOR", "workspace_file_locator_policy", "implement")
    ]
    assert "blob read failed" in preview.diagnostics[0].message
    assert "Git command timed out" in preview.diagnostics[0].message


def test_workspace_dynamic_file_locator_does_not_follow_checkout_symlink(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    try:
        (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    workflow = WorkflowPlan(
        name="workspace dynamic locator",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
            ),
            WorkflowNode(
                id="fix",
                mode="sequential",
                needs=["implement"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Read {{file:linked/future.md}}",
                    )
                ],
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        workspace_source_snapshot("a" * 40),
    )

    assert preview.diagnostics == []
    assert len(preview.workspace_file_locators) == 2
    locator = preview.workspace_file_locators[0]
    assert locator.node_id == "fix"
    assert locator.source_class == "runtime_dynamic"
    assert locator.workspace_relative_path == "linked/future.md"
    assert locator.git_top_relative_path == "linked/future.md"
    assert locator.git_blob is None
