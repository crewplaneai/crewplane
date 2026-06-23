from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import crewplane.core.preflight.references as preflight_references
import crewplane.core.workflow.models as workflow_models
import crewplane.core.workflow.validation as workflow_validation
from crewplane.core.preflight.models import (
    ArtifactContract,
    ExecutionPolicy,
    Fragment,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
    RenderStream,
    WorkspaceFileLocator,
    WorkspaceSourceSnapshot,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.fragment_assembler import assemble_prompt
from crewplane.runtime.execution.workspace_files import resolve_workspace_file
from crewplane.version import SCHEMA_VERSION
from tests.helpers.workspace_records import (
    WORKTREE_CONTRACT,
    workspace_selection_record,
)


class _ArtifactStore:
    run_id = "run"
    task_name = "demo"
    log_cli_output = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.stages_dir = root / "stages"
        self.results_dir = root / "results"
        self.logs_dir = root / "logs"
        self.stages_dir.mkdir(parents=True)
        self.results_dir.mkdir(parents=True)

    def get_stage_output_path(self, stage_name: str) -> Path:
        return self.results_dir / f"{stage_name}-result.md"

    def get_stage_findings_path(self, stage_name: str) -> Path:
        return self.results_dir / f"{stage_name}-findings.md"

    def get_stage_dir(self, stage_name: str) -> Path | None:
        path = self.stages_dir / stage_name
        return path if path.is_dir() else None


def _static_content_ref(payload: bytes) -> str:
    return f"static-files/{hashlib.sha256(payload).hexdigest()}.txt"


def _write_lineage_state_with_bundle(
    repo: Path,
    stage_dir: Path,
    result_commit: str,
    result_tree: str,
    result_ref: str,
    bundle_name: str,
    extra_payload: dict[str, object] | None = None,
) -> None:
    bundle_dir = stage_dir / "workspace-bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / bundle_name
    _git(repo, "update-ref", result_ref, result_commit)
    _git(repo, "bundle", "create", bundle_path.as_posix(), result_ref)
    bundle_sha256 = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    state_path = stage_dir / "workspace-state.json"
    payload: dict[str, object] = {
        "status": "succeeded",
        "role": "executor",
        "workspace": {"lineage_producer": True},
        "result": {
            "result_commit": result_commit,
            "result_tree": result_tree,
        },
        "refs": {"result": result_ref},
        "bundle": {
            "path": bundle_path.relative_to(state_path.parent.parent).as_posix(),
            "sha256": bundle_sha256,
            "size_bytes": bundle_path.stat().st_size,
            "verified": True,
        },
    }
    if extra_payload is not None:
        payload.update(extra_payload)
    state_path.write_text(json.dumps(payload), encoding="utf-8")


def _plan(root: Path, content_ref: str | None = None) -> PreflightExecutionPlan:
    static_content_ref = content_ref or _static_content_ref(b"file")
    upstream = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(output_path="compiled-input.md"),
        execution_policy=ExecutionPolicy(),
        input_content_ref="static-files/input.txt",
    )
    node = PreflightExecutionNode(
        id="build",
        mode="sequential",
        render_plan_id="build",
        artifact_contract=ArtifactContract(output_path="build-result.md"),
        execution_policy=ExecutionPolicy(),
        provider_records=[
            ProviderRecord(
                provider="mock",
                role=ProviderRole.EXECUTOR,
                task_id="mock_executor_0",
                agent_config_key="mock",
                invoker_alias="mock",
                agent_config_signature="agent-signature",
                invoker_config_signature="invoker-signature",
            )
        ],
    )
    return PreflightExecutionPlan(
        run_id="run",
        run_key_name="demo-run",
        project_root=root.as_posix(),
        context_root=root.as_posix(),
        manifest_root=(root / "manifests").as_posix(),
        created_at="2026-06-03T00:00:00",
        workflow_name="demo",
        workflow_signature="0" * 64,
        execution_order=["input", "build"],
        nodes=[upstream, node],
        render_plans=[
            RenderPlan(
                render_plan_id="build",
                streams=[
                    RenderStream(
                        target_role=ProviderRole.EXECUTOR,
                        fragments=[
                            Fragment(
                                fragment_index=0,
                                kind="literal",
                                source_role=PromptSegmentRole.SHARED,
                                text="A ",
                            ),
                            Fragment(
                                fragment_index=1,
                                kind="static_file_content",
                                source_role=PromptSegmentRole.SHARED,
                                content_ref=static_content_ref,
                            ),
                            Fragment(
                                fragment_index=2,
                                kind="literal",
                                source_role=PromptSegmentRole.SHARED,
                                text=" B ",
                            ),
                            Fragment(
                                fragment_index=3,
                                kind="runtime_locator_lookup",
                                source_role=PromptSegmentRole.SHARED,
                                locator={
                                    "node_id": "input",
                                    "artifact_name": "output",
                                },
                            ),
                            Fragment(
                                fragment_index=4,
                                kind="literal",
                                source_role=PromptSegmentRole.SHARED,
                                text=" C ",
                            ),
                            Fragment(
                                fragment_index=5,
                                kind="static_env",
                                source_role=PromptSegmentRole.SHARED,
                                key="API_TOKEN",
                                value_handle="env:API_TOKEN",
                            ),
                        ],
                    )
                ],
            )
        ],
        static_resources=[],
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        effective_runtime_config_signature="1" * 64,
        fingerprint_metadata={"payload_version": "1"},
    )


def test_assemble_prompt_preserves_fragment_order(tmp_path: Path) -> None:
    context_root = tmp_path / "execution-stages" / "demo-run"
    content_ref = _static_content_ref(b"file")
    static_path = context_root / "preflight" / content_ref
    static_path.parent.mkdir(parents=True)
    static_path.write_text("file", encoding="utf-8")

    store = _ArtifactStore(tmp_path)
    (store.results_dir / "compiled-input.md").write_text("node", encoding="utf-8")

    secrets = SecretContext()
    secrets.put("env:API_TOKEN", "secret")

    plan = _plan(context_root)
    prompt = assemble_prompt(plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets)

    assert prompt == "A file B node C secret"


def test_assemble_prompt_does_not_call_legacy_template_parsers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail() -> None:
        raise AssertionError("runtime must not parse template tokens")

    monkeypatch.setattr(preflight_references, "iter_template_references", fail)
    monkeypatch.setattr(workflow_validation, "extract_template_tokens", fail)
    monkeypatch.setattr(workflow_models, "render_prompt_for_role", fail)

    context_root = tmp_path / "execution-stages" / "demo-run"
    content_ref = _static_content_ref(b"file")
    static_path = context_root / "preflight" / content_ref
    static_path.parent.mkdir(parents=True)
    static_path.write_text("file", encoding="utf-8")
    store = _ArtifactStore(tmp_path)
    (store.results_dir / "compiled-input.md").write_text("node", encoding="utf-8")
    secrets = SecretContext()
    secrets.put("env:API_TOKEN", "secret")
    plan = _plan(context_root)

    prompt = assemble_prompt(plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets)

    assert prompt == "A file B node C secret"


def test_assemble_prompt_reads_static_bundle_not_original_source_path(
    tmp_path: Path,
) -> None:
    context_root = tmp_path / "execution-stages" / "demo-run"
    payload = b"bundled"
    content_sha256 = hashlib.sha256(payload).hexdigest()
    content_ref = f"static-files/{content_sha256}.txt"
    static_path = context_root / "preflight" / content_ref
    static_path.parent.mkdir(parents=True)
    static_path.write_bytes(payload)
    plan = _plan(context_root, content_ref=content_ref).model_copy(
        update={
            "static_resources": [
                {
                    "resource_id": content_sha256,
                    "kind": "file",
                    "raw_path": "deleted.md",
                    "source_root": (tmp_path / "source").as_posix(),
                    "resolved_path": (tmp_path / "source" / "deleted.md").as_posix(),
                    "content_ref": content_ref,
                    "size_bytes": len(payload),
                    "sha256": content_sha256,
                }
            ]
        }
    )
    store = _ArtifactStore(tmp_path)
    (store.results_dir / "compiled-input.md").write_text("node", encoding="utf-8")
    secrets = SecretContext()
    secrets.put("env:API_TOKEN", "secret")

    prompt = assemble_prompt(plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets)

    assert prompt == "A bundled B node C secret"


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
    plan = _plan(context_root).model_copy(
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
    store = _ArtifactStore(tmp_path)
    secrets = SecretContext()

    prompt = assemble_prompt(plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets)

    assert prompt == "workspace file"


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
    plan = _plan(context_root).model_copy(update={"workspace_file_locators": [locator]})
    store = _ArtifactStore(tmp_path)

    resolved = resolve_workspace_file(
        plan,
        store,
        "workspace-file-reviewer",
        workspace_candidate_source=True,
    )

    assert resolved.text == "snapshot reviewer file"


def test_assemble_prompt_rejects_runtime_dynamic_workspace_file_locator(
    tmp_path: Path,
) -> None:
    context_root = tmp_path / "execution-stages" / "demo-run"
    plan = _plan(context_root).model_copy(
        update={
            "workspace_file_locators": [
                WorkspaceFileLocator(
                    locator_id="workspace-file-dynamic",
                    occurrence_id="build:executor:0:file:future.md",
                    node_id="build",
                    target="executor_prompt",
                    source_class="runtime_dynamic",
                    raw_token="{{file:future.md}}",
                    raw_path="future.md",
                    source_root=tmp_path.as_posix(),
                    source_root_relative_to_project=".",
                    project_root_relative_to_git_top=".",
                    git_top_relative_path="future.md",
                    workspace_relative_path="future.md",
                )
            ],
            "render_plans": [
                RenderPlan(
                    render_plan_id="build",
                    streams=[
                        RenderStream(
                            target_role=ProviderRole.EXECUTOR,
                            fragments=[
                                Fragment(
                                    fragment_index=0,
                                    kind="workspace_file_locator",
                                    source_role=PromptSegmentRole.SHARED,
                                    locator={
                                        "locator_id": "workspace-file-dynamic",
                                        "source_class": "runtime_dynamic",
                                        "workspace_relative_path": "future.md",
                                    },
                                )
                            ],
                        )
                    ],
                )
            ],
        }
    )
    store = _ArtifactStore(tmp_path)
    secrets = SecretContext()

    with pytest.raises(RuntimeError, match="Runtime-dynamic workspace file locator"):
        assemble_prompt(plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets)


def test_assemble_prompt_reads_runtime_dynamic_workspace_file_locator(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Crewplane Test")
    _git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "future.md").write_text("dynamic\n", encoding="utf-8")
    _git(repo, "add", "future.md")
    _git(repo, "commit", "-m", "candidate")
    result_commit = _git(repo, "rev-parse", "HEAD^{commit}")
    result_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = _ArtifactStore(tmp_path)
    upstream_stage = store.stages_dir / "input"
    _write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=upstream_stage,
        result_commit=result_commit,
        result_tree=result_tree,
        result_ref="refs/crewplane/tests/input/result",
        bundle_name="input.bundle",
        extra_payload={"node_id": "input"},
    )
    locator = WorkspaceFileLocator(
        locator_id="workspace-file-dynamic",
        occurrence_id="build:executor:0:file:future.md",
        node_id="build",
        target="executor_prompt",
        source_class="runtime_dynamic",
        raw_token="{{file:future.md}}",
        raw_path="future.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="future.md",
        workspace_relative_path="future.md",
    )
    plan = _plan(context_root).model_copy(
        update={
            "workspace_source": WorkspaceSourceSnapshot(
                worktree_contract=WORKTREE_CONTRACT,
                run_base_commit=result_commit,
                source_tree=result_tree,
                object_format="sha1",
                repository_id="repo",
                git_version=_git(repo, "--version"),
                git_top_level=repo.as_posix(),
                project_root_relative_path=".",
                active_git_dir=(repo / ".git").as_posix(),
                common_git_dir=(repo / ".git").as_posix(),
                clean_start="strict",
            ),
            "workspace_file_locators": [locator],
            "nodes": [
                _plan(context_root).nodes[0],
                _plan(context_root)
                .nodes[1]
                .model_copy(
                    update={
                        "workspace_policy": workspace_selection_record(
                            enabled=True,
                            kind="worktree",
                            source_kind="node",
                            source_node_id="input",
                            clean_start="strict",
                            materialization="worktree_checkout",
                        )
                    }
                ),
            ],
            "render_plans": [
                RenderPlan(
                    render_plan_id="build",
                    streams=[
                        RenderStream(
                            target_role=ProviderRole.EXECUTOR,
                            fragments=[
                                Fragment(
                                    fragment_index=0,
                                    kind="workspace_file_locator",
                                    source_role=PromptSegmentRole.SHARED,
                                    locator={
                                        "locator_id": "workspace-file-dynamic",
                                        "source_class": "runtime_dynamic",
                                        "workspace_relative_path": "future.md",
                                    },
                                )
                            ],
                        )
                    ],
                )
            ],
        }
    )

    prompt = assemble_prompt(
        plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
    )

    assert prompt == "dynamic\n"


def test_assemble_prompt_imports_bundle_for_runtime_dynamic_workspace_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Crewplane Test")
    _git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    parent_commit = _git(repo, "rev-parse", "HEAD^{commit}")
    parent_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    (repo / "future.md").write_text("bundled dynamic\n", encoding="utf-8")
    _git(repo, "add", "future.md")
    result_tree = _git(repo, "write-tree")
    result_commit = _git(
        repo,
        "commit-tree",
        result_tree,
        "-p",
        parent_commit,
        "-m",
        "candidate",
    )
    _git(repo, "reset", "--hard", parent_commit)

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = _ArtifactStore(tmp_path)
    upstream_stage = store.stages_dir / "input"
    bundle_dir = upstream_stage / "workspace-bundles"
    bundle_dir.mkdir(parents=True)
    result_ref = "refs/crewplane/tests/input/result"
    bundle_path = bundle_dir / "input.bundle"
    _git(repo, "update-ref", result_ref, result_commit)
    _git(repo, "bundle", "create", bundle_path.as_posix(), result_ref)
    bundle_sha256 = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    _git(repo, "update-ref", "-d", result_ref)
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now")
    if _git_commit_exists(repo, result_commit):
        pytest.skip("git retained the test commit after pruning")

    (upstream_stage / "workspace-state.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "role": "executor",
                "node_id": "input",
                "workspace": {"lineage_producer": True},
                "result": {
                    "result_commit": result_commit,
                    "result_tree": result_tree,
                },
                "refs": {"result": result_ref},
                "bundle": {
                    "path": bundle_path.relative_to(store.stages_dir).as_posix(),
                    "sha256": bundle_sha256,
                    "size_bytes": bundle_path.stat().st_size,
                    "verified": True,
                },
            }
        ),
        encoding="utf-8",
    )
    locator = WorkspaceFileLocator(
        locator_id="workspace-file-dynamic",
        occurrence_id="build:executor:0:file:future.md",
        node_id="build",
        target="executor_prompt",
        source_class="runtime_dynamic",
        raw_token="{{file:future.md}}",
        raw_path="future.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="future.md",
        workspace_relative_path="future.md",
    )
    plan = _plan(context_root).model_copy(
        update={
            "workspace_source": WorkspaceSourceSnapshot(
                worktree_contract=WORKTREE_CONTRACT,
                run_base_commit=parent_commit,
                source_tree=parent_tree,
                object_format="sha1",
                repository_id="repo",
                git_version=_git(repo, "--version"),
                git_top_level=repo.as_posix(),
                project_root_relative_path=".",
                active_git_dir=(repo / ".git").as_posix(),
                common_git_dir=(repo / ".git").as_posix(),
                clean_start="strict",
            ),
            "workspace_file_locators": [locator],
            "nodes": [
                _plan(context_root).nodes[0],
                _plan(context_root)
                .nodes[1]
                .model_copy(
                    update={
                        "workspace_policy": workspace_selection_record(
                            enabled=True,
                            kind="worktree",
                            source_kind="node",
                            source_node_id="input",
                            clean_start="strict",
                            materialization="worktree_checkout",
                        )
                    }
                ),
            ],
            "render_plans": [
                RenderPlan(
                    render_plan_id="build",
                    streams=[
                        RenderStream(
                            target_role=ProviderRole.EXECUTOR,
                            fragments=[
                                Fragment(
                                    fragment_index=0,
                                    kind="workspace_file_locator",
                                    source_role=PromptSegmentRole.SHARED,
                                    locator={
                                        "locator_id": "workspace-file-dynamic",
                                        "source_class": "runtime_dynamic",
                                        "workspace_relative_path": "future.md",
                                    },
                                )
                            ],
                        )
                    ],
                )
            ],
        }
    )

    prompt = assemble_prompt(
        plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
    )

    assert prompt == "bundled dynamic\n"
    assert _git_commit_exists(repo, result_commit)


def test_assemble_prompt_reads_after_candidate_workspace_locator_from_candidate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Crewplane Test")
    _git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "future.md").write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "future.md")
    _git(repo, "commit", "-m", "candidate")
    result_commit = _git(repo, "rev-parse", "HEAD^{commit}")
    result_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    content_ref = "workspace-files/workspace-file-after-candidate.txt"
    preflight_file = context_root / "preflight" / content_ref
    preflight_file.parent.mkdir(parents=True)
    preflight_file.write_text("base\n", encoding="utf-8")
    store = _ArtifactStore(tmp_path)
    build_stage = store.stages_dir / "build"
    _write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=build_stage,
        result_commit=result_commit,
        result_tree=result_tree,
        result_ref="refs/crewplane/tests/build/result",
        bundle_name="build.bundle",
        extra_payload={
            "node_id": "build",
            "task_id": "alpha",
            "round_num": 1,
            "audit_round_num": None,
        },
    )
    locator = WorkspaceFileLocator(
        locator_id="workspace-file-after-candidate",
        content_ref=content_ref,
        occurrence_id="build:executor:0:file:future.md",
        node_id="build",
        target="executor_prompt",
        source_class="project_initial",
        raw_token="{{file:future.md}}",
        raw_path="future.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="future.md",
        workspace_relative_path="future.md",
        runtime_dynamic_after_candidate=True,
        byte_size=len("base\n"),
        canonical_blob_sha256=hashlib.sha256(b"base\n").hexdigest(),
    )
    plan = _plan(context_root).model_copy(
        update={
            "workspace_source": WorkspaceSourceSnapshot(
                worktree_contract=WORKTREE_CONTRACT,
                run_base_commit=result_commit,
                source_tree=result_tree,
                object_format="sha1",
                repository_id="repo",
                git_version=_git(repo, "--version"),
                git_top_level=repo.as_posix(),
                project_root_relative_path=".",
                active_git_dir=(repo / ".git").as_posix(),
                common_git_dir=(repo / ".git").as_posix(),
                clean_start="strict",
            ),
            "workspace_file_locators": [locator],
            "nodes": [
                _plan(context_root).nodes[0],
                _plan(context_root)
                .nodes[1]
                .model_copy(
                    update={
                        "workspace_policy": workspace_selection_record(
                            enabled=True,
                            kind="worktree",
                            clean_start="strict",
                            materialization="worktree_checkout",
                        )
                    }
                ),
            ],
            "render_plans": [
                RenderPlan(
                    render_plan_id="build",
                    streams=[
                        RenderStream(
                            target_role=ProviderRole.EXECUTOR,
                            fragments=[
                                Fragment(
                                    fragment_index=0,
                                    kind="workspace_file_locator",
                                    source_role=PromptSegmentRole.SHARED,
                                    locator={
                                        "locator_id": "workspace-file-after-candidate",
                                        "source_class": "project_initial",
                                        "workspace_relative_path": "future.md",
                                    },
                                )
                            ],
                        )
                    ],
                )
            ],
        }
    )

    assert (
        assemble_prompt(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )
        == "base\n"
    )
    assert (
        assemble_prompt(
            plan,
            plan.nodes[1],
            ProviderRole.EXECUTOR,
            store,
            SecretContext(),
            workspace_candidate_source=True,
        )
        == "candidate\n"
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8").strip()


def _git_commit_exists(repo: Path, object_id: str) -> bool:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), "cat-file", "-e", f"{object_id}^{{commit}}"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0
