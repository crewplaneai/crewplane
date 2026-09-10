from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from crewplane.core.preflight.models import (
    Fragment,
    RenderPlan,
    RenderStream,
    WorkspaceFileLocator,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.fragment_assembler import assemble_prompt
from crewplane.runtime.execution.workspace_files import (
    resolve_project_initial_workspace_file,
    resolve_workspace_file,
)
from tests.integration.runtime.execution.fragment_assembler_support import (
    FragmentArtifactStore,
    make_fragment_plan,
)


def test_assemble_prompt_reads_project_initial_workspace_file_locator(
    tmp_path: Path,
) -> None:
    context_root = tmp_path / "execution-stages" / "demo-run"
    payload = b"workspace file"
    digest = hashlib.sha256(payload).hexdigest()
    content_ref = "workspace-files/workspace-file-test.txt"
    workspace_file = context_root / "preflight" / content_ref
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_bytes(payload)
    plan = make_fragment_plan(context_root).model_copy(
        update={
            "workspace_file_locators": [
                WorkspaceFileLocator(
                    locator_id="workspace-file-test",
                    content_ref=content_ref,
                    occurrence_id="build:executor:0:file:README.md",
                    node_id="build",
                    target="executor_prompt",
                    source_class="project_initial",
                    raw_token="{{file:README.md}}",
                    raw_path="README.md",
                    source_root=tmp_path.as_posix(),
                    source_root_relative_to_project=".",
                    project_root_relative_to_git_top=".",
                    git_top_relative_path="README.md",
                    workspace_relative_path="README.md",
                    git_blob="a" * 40,
                    git_file_mode="100644",
                    byte_size=len(payload),
                    canonical_blob_sha256=digest,
                    literal_path_verified=True,
                    utf8_validated=True,
                )
            ],
            "render_plans": [
                RenderPlan(
                    render_plan_id="build",
                    node_id="build",
                    streams=[
                        RenderStream(
                            target_role=ProviderRole.EXECUTOR,
                            fragments=[
                                Fragment(
                                    fragment_index=0,
                                    kind="workspace_file_locator",
                                    source_role=PromptSegmentRole.SHARED,
                                    locator={
                                        "locator_id": "workspace-file-test",
                                        "source_class": "project_initial",
                                        "workspace_relative_path": "README.md",
                                    },
                                )
                            ],
                        )
                    ],
                )
            ],
        }
    )
    store = FragmentArtifactStore(tmp_path)
    secrets = SecretContext()

    prompt = assemble_prompt(plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets)

    assert prompt == "workspace file"


def test_project_initial_workspace_file_preserves_validation_precedence(
    tmp_path: Path,
) -> None:
    context_root = tmp_path / "execution-stages" / "demo-run"
    content_ref = "workspace-files/invalid.txt"
    payload = b"\xff"
    workspace_file = context_root / "preflight" / content_ref
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_bytes(payload)
    locator = WorkspaceFileLocator(
        locator_id="workspace-file-test",
        content_ref=content_ref,
        occurrence_id="build:executor:0:file:README.md",
        node_id="build",
        target="executor_prompt",
        source_class="project_initial",
        raw_token="{{file:README.md}}",
        raw_path="README.md",
        source_root=tmp_path.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="README.md",
        workspace_relative_path="README.md",
        git_blob="a" * 40,
        git_file_mode="100644",
        byte_size=len(payload),
        canonical_blob_sha256=hashlib.sha256(payload).hexdigest(),
        literal_path_verified=True,
        utf8_validated=True,
    )

    def resolve(candidate: WorkspaceFileLocator) -> None:
        plan = make_fragment_plan(context_root).model_copy(
            update={"workspace_file_locators": [candidate]}
        )
        resolve_project_initial_workspace_file(plan, candidate.locator_id)

    with pytest.raises(RuntimeError, match="Runtime-dynamic"):
        resolve(
            locator.model_copy(
                update={"source_class": "runtime_dynamic", "content_ref": None}
            )
        )
    with pytest.raises(RuntimeError, match="missing preflight content"):
        resolve(locator.model_copy(update={"content_ref": None}))
    with pytest.raises(ValueError, match="Invalid workspace content reference"):
        resolve(
            locator.model_copy(
                update={
                    "content_ref": "../outside.txt",
                    "canonical_blob_sha256": "0" * 64,
                    "byte_size": 2,
                }
            )
        )
    with pytest.raises(RuntimeError, match="content digest mismatch"):
        resolve(
            locator.model_copy(
                update={"canonical_blob_sha256": "0" * 64, "byte_size": 2}
            )
        )
    with pytest.raises(RuntimeError, match="content size mismatch"):
        resolve(locator.model_copy(update={"byte_size": 2}))
    with pytest.raises(RuntimeError, match="not valid UTF-8"):
        resolve(locator)


def test_reviewer_prompt_reads_project_initial_workspace_file_locator(
    tmp_path: Path,
) -> None:
    context_root = tmp_path / "execution-stages" / "demo-run"
    payload = b"snapshot reviewer file"
    digest = hashlib.sha256(payload).hexdigest()
    content_ref = "workspace-files/workspace-file-reviewer.txt"
    workspace_file = context_root / "preflight" / content_ref
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_bytes(payload)
    locator = WorkspaceFileLocator(
        locator_id="workspace-file-reviewer",
        content_ref=content_ref,
        occurrence_id="build:reviewer:0:file:README.md",
        node_id="build",
        target="reviewer_prompt",
        source_class="project_initial",
        raw_token="{{file:README.md}}",
        raw_path="README.md",
        source_root=tmp_path.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="README.md",
        workspace_relative_path="README.md",
        git_blob="a" * 40,
        git_file_mode="100644",
        byte_size=len(payload),
        canonical_blob_sha256=digest,
        literal_path_verified=True,
        utf8_validated=True,
    )
    plan = make_fragment_plan(context_root).model_copy(
        update={"workspace_file_locators": [locator]}
    )
    store = FragmentArtifactStore(tmp_path)

    resolved = resolve_workspace_file(
        plan,
        store,
        "workspace-file-reviewer",
        workspace_candidate_source=True,
    )

    assert resolved.text == "snapshot reviewer file"
