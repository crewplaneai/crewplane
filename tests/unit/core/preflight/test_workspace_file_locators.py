import hashlib
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from orchestrator_cli.bootstrap import build_runtime_config_snapshot
from orchestrator_cli.core.preflight import (
    PreflightCompileOptions,
    compile_preflight_preview,
    load_workflow_source_for_preflight,
    workspace_git_file_reads,
)
from orchestrator_cli.core.prompt_segments import PromptSegment
from orchestrator_cli.core.workflow_models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.version import SCHEMA_VERSION
from tests.helpers.workspace_preflight import (
    compile_workflow_with_source_snapshot,
    init_git_repo,
    workspace_config,
    workspace_source_snapshot,
    workspace_workflow,
)


def test_workspace_enabled_file_tokens_compile_to_workspace_locators(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow("Read {{file:docs/requirements.md}}"),
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.workflow_signature is not None
    assert len(preview.workspace_file_locators) == 2
    project_locator = preview.workspace_file_locators[0]
    reviewer_locator = preview.workspace_file_locators[1]
    assert project_locator.source_class == "project_initial"
    assert project_locator.target == "executor_prompt"
    assert project_locator.runtime_dynamic_after_candidate is True
    assert project_locator.workspace_relative_path == "docs/requirements.md"
    assert project_locator.git_top_relative_path == "docs/requirements.md"
    assert project_locator.content_ref is not None
    assert project_locator.content_ref.startswith("workspace-files/")
    assert project_locator.git_blob is not None
    assert project_locator.git_file_mode == "100644"
    assert project_locator.byte_size == len(b"requirements\n")
    assert (
        project_locator.canonical_blob_sha256
        == hashlib.sha256(b"requirements\n").hexdigest()
    )
    assert project_locator.literal_path_verified is True
    assert project_locator.utf8_validated is True
    assert preview.workspace_file_payloads == {
        project_locator.content_ref: b"requirements\n"
    }
    assert reviewer_locator.source_class == "runtime_dynamic"
    assert reviewer_locator.content_ref is None
    assert reviewer_locator.git_blob is None
    assert preview.render_plans[0].streams[0].fragments[1].kind == (
        "workspace_file_locator"
    )
    assert preview.token_catalog[0].canonical_locator == project_locator.locator_id


def test_workspace_imported_file_token_resolves_from_project_root(
    tmp_path: Path,
) -> None:
    root = tmp_path
    child_dir = root / ".orchestrator" / "workflows" / "child"
    root_requirements = root / "docs" / "requirements.md"
    child_requirements = child_dir / "docs" / "requirements.md"
    root_requirements.parent.mkdir()
    child_requirements.parent.mkdir(parents=True)
    root_requirements.write_text("project requirements\n", encoding="utf-8")
    child_requirements.write_text("child requirements\n", encoding="utf-8")
    (child_dir / "workflow.task.md").write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Child",
                "worktrees:",
                "  primary:",
                "    kind: worktree",
                "nodes:",
                "  - id: implement",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## implement",
                "",
                "Read {{file:docs/requirements.md}}",
            ]
        ),
        encoding="utf-8",
    )
    root_workflow = root / ".orchestrator" / "workflows" / "root.task.md"
    root_workflow.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Root",
                "imports:",
                "  - path: child/workflow.task.md",
                "    as: child",
                "nodes: []",
                "---",
            ]
        ),
        encoding="utf-8",
    )
    source_snapshot = init_git_repo(root)
    source = load_workflow_source_for_preflight(root_workflow, project_root=root)
    config = workspace_config()
    runtime_snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )

    preview = compile_preflight_preview(
        source=source,
        config=config,
        runtime_snapshot=runtime_snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            orchestrator_dir=root / ".orchestrator",
            fingerprint_key_policy="read_only",
            workspace_source_snapshot=source_snapshot,
        ),
    )

    assert preview.diagnostics == []
    assert {
        locator.workspace_relative_path for locator in preview.workspace_file_locators
    } == {"docs/requirements.md"}
    assert {
        locator.git_top_relative_path for locator in preview.workspace_file_locators
    } == {"docs/requirements.md"}
    locator = next(
        locator
        for locator in preview.workspace_file_locators
        if locator.source_class == "project_initial"
    )
    assert locator.source_root == root.as_posix()
    assert locator.source_root_relative_to_project == "."
    assert locator.workspace_relative_path == "docs/requirements.md"
    assert locator.git_top_relative_path == "docs/requirements.md"
    assert locator.content_ref is not None
    assert preview.workspace_file_payloads == {
        locator.content_ref: b"project requirements\n"
    }


def test_downstream_worktree_executor_file_locator_can_use_candidate_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("ready\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace remediation file",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="implement")],
            ),
            WorkflowNode(
                id="fix",
                mode="sequential",
                needs=["implement"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Fix using {{file:docs/generated.md}}",
                    )
                ],
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    executor_locators = [
        locator
        for locator in preview.workspace_file_locators
        if locator.target == "executor_prompt"
    ]
    assert len(executor_locators) == 1
    locator = executor_locators[0]
    assert locator.node_id == "fix"
    assert locator.source_class == "runtime_dynamic"
    assert locator.target == "executor_prompt"
    assert locator.runtime_dynamic_after_candidate is True
    assert locator.content_ref is None


def test_worktree_executor_file_locator_after_project_root_node_uses_prior_candidate(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("ready\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace transitive file",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="implement")],
            ),
            WorkflowNode(
                id="inspect",
                mode="sequential",
                needs=["implement"],
                worktree="none",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="inspect")],
            ),
            WorkflowNode(
                id="fix",
                mode="sequential",
                needs=["inspect"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Fix using {{file:README.md}}",
                    )
                ],
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.workflow_signature is not None
    executor_locators = [
        locator
        for locator in preview.workspace_file_locators
        if locator.target == "executor_prompt"
    ]
    assert len(executor_locators) == 1
    locator = executor_locators[0]
    assert locator.node_id == "fix"
    assert locator.source_class == "runtime_dynamic"
    assert locator.runtime_dynamic_after_candidate is True
    assert locator.content_ref is None


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


def test_workspace_enabled_unresolved_home_file_locator_reports_diagnostic(
    tmp_path: Path,
) -> None:
    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow(
            "Read {{file:~orchestrator_cli_missing_user_for_tests/context.md}}"
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


def test_workspace_enabled_input_node_uses_static_file_content(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace input",
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/requirements.md}}",
            )
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.workspace_file_locators == []
    assert preview.workspace_file_payloads == {}
    assert preview.nodes[0].input_content_ref is not None
    assert preview.nodes[0].input_workspace_file_locator_id is None
    assert preview.nodes[0].workspace_policy is None


def test_workspace_enabled_input_node_stays_static_when_provider_opts_out(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace input",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/requirements.md}}",
            ),
            WorkflowNode(
                id="implement",
                mode="sequential",
                needs=["requirements"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="run")],
                worktree="none",
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.workspace_file_locators == []
    assert preview.nodes[0].input_content_ref is not None
    assert preview.nodes[0].input_workspace_file_locator_id is None
    assert preview.nodes[1].workspace_policy is None


def test_workspace_enabled_input_node_uses_workspace_file_locator(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace input",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/requirements.md}}",
            ),
            WorkflowNode(
                id="implement",
                mode="sequential",
                needs=["requirements"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="run")],
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.static_resources == []
    assert len(preview.workspace_file_locators) == 1
    locator = preview.workspace_file_locators[0]
    assert locator.node_id == "requirements"
    assert locator.target == "input_output"
    assert locator.source_class == "project_initial"
    assert locator.workspace_relative_path == "docs/requirements.md"
    assert locator.git_top_relative_path == "docs/requirements.md"
    assert locator.content_ref is not None
    assert preview.workspace_file_payloads == {locator.content_ref: b"requirements\n"}
    assert preview.nodes[0].input_content_ref is None
    assert preview.nodes[0].input_workspace_file_locator_id == locator.locator_id
    assert preview.nodes[0].workspace_policy is None
    assert preview.nodes[1].workspace_policy is not None
    assert preview.token_catalog[0].canonical_locator == locator.locator_id
    assert preview.token_catalog[0].resolved["kind"] == "workspace_file_locator"


def test_worktree_node_after_input_uses_project_initial_file_locator(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    generated = tmp_path / "docs" / "generated.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    generated.write_text("generated\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace input then implement",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/requirements.md}}",
            ),
            WorkflowNode(
                id="implement",
                mode="sequential",
                needs=["requirements"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Read {{file:docs/generated.md}}",
                    )
                ],
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    implement_locator = next(
        locator
        for locator in preview.workspace_file_locators
        if locator.node_id == "implement"
    )
    assert implement_locator.source_class == "project_initial"
    assert implement_locator.content_ref is not None
    assert preview.workspace_file_payloads[implement_locator.content_ref] == (
        b"generated\n"
    )


def test_workspace_enabled_allowlisted_absolute_input_source_remains_static(
    tmp_path: Path,
) -> None:
    external_dir = tmp_path.parent / f"{tmp_path.name}-external"
    external_dir.mkdir()
    external_file = external_dir / "requirements.md"
    external_file.write_text("external requirements\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace input",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source=f"{{{{file:{external_file.as_posix()}}}}}",
            ),
            WorkflowNode(
                id="implement",
                mode="sequential",
                needs=["requirements"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="run")],
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
        allowed_template_paths=(external_dir,),
    )

    assert preview.diagnostics == []
    assert preview.workspace_file_locators == []
    assert [resource.raw_path for resource in preview.static_resources] == [
        external_file.as_posix()
    ]
    assert preview.nodes[0].input_content_ref is not None
    assert preview.nodes[0].input_workspace_file_locator_id is None


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
                prompt_segments=[PromptSegment(role="shared", content="run")],
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
