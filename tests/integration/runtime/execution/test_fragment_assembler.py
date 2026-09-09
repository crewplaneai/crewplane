from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import crewplane.core.preflight.references as preflight_references
import crewplane.core.workflow.models as workflow_models
import crewplane.core.workflow.validation as workflow_validation
import crewplane.runtime.execution.workspace_files.resolution as workspace_file_resolution
from crewplane.architecture.contracts import NodeArtifactRequest, VerifiedNodeArtifact
from crewplane.artifacts.verification import read_verified_node_artifact
from crewplane.core.preflight.dependency_edges import dependency_signature
from crewplane.core.preflight.models import (
    ArtifactContract,
    DependencyEdge,
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
from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.fragment_assembler import assemble_prompt
from crewplane.runtime.execution.workspace_files import (
    WorkspaceCandidateSourceContext,
    resolve_project_initial_workspace_file,
    resolve_workspace_file,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import (
    make_node_state,
    make_run_manifest,
    write_node_state,
    write_result,
)
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

    def get_stage_dir(self, stage_name: str) -> Path | None:
        path = self.stages_dir / stage_name
        return path if path.is_dir() else None

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        stage_path = request.contract.stage_path
        assert stage_path is not None
        path = self.stages_dir / stage_path
        return path if path.is_dir() else None

    def get_run_log_dir(self) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        return self.logs_dir

    def read_verified_node_artifact(
        self,
        request: NodeArtifactRequest,
        kind: str,
    ) -> VerifiedNodeArtifact:
        relative_path = (
            request.contract.output_path
            if kind == "output"
            else request.contract.findings_path
        )
        assert relative_path is not None
        path = self.results_dir / relative_path
        payload = path.read_bytes()
        return VerifiedNodeArtifact(
            path=path,
            payload=payload,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )


def _static_content_ref(payload: bytes) -> str:
    return f"static-files/{hashlib.sha256(payload).hexdigest()}.txt"


def _write_lineage_state_with_bundle(
    repo: Path,
    stage_dir: Path,
    source_commit: str,
    source_tree: str,
    result_commit: str,
    result_tree: str,
    bundle_name: str,
    extra_payload: dict[str, object] | None = None,
) -> str:
    overrides = extra_payload or {}
    node_id = str(overrides.get("node_id", "input"))
    task_id = str(overrides.get("task_id", "mock_executor_0"))
    round_num = int(overrides.get("round_num", 1))
    audit_round_num = overrides.get("audit_round_num")
    slug = invocation_slug(
        node_id,
        task_id,
        audit_round_num if isinstance(audit_round_num, int) else None,
        round_num,
    )
    result_ref = f"refs/crewplane/runs/demo-run/{node_id}/{slug}/result"
    bundle_dir = stage_dir / "workspace-bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / bundle_name
    _git(repo, "update-ref", result_ref, result_commit)
    _git(repo, "bundle", "create", bundle_path.as_posix(), result_ref)
    bundle_sha256 = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    state_path = stage_dir / "workspace-state.json"
    candidate_ref = f"{result_ref.removesuffix('/result')}/candidate"
    payload: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "run_id": "run",
        "run_key_name": "demo-run",
        "workflow_name": "demo",
        "workflow_signature": "0" * 64,
        "node_id": node_id,
        "task_id": task_id,
        "provider": "mock",
        "status": "succeeded",
        "role": "executor",
        "round_num": round_num,
        "audit_round_num": audit_round_num,
        "workspace_kind": "worktree",
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": WORKTREE_CONTRACT.model_dump(mode="json"),
        "git": {
            "object_format": _git(repo, "rev-parse", "--show-object-format=storage"),
            "repo_id": "repo",
            "run_base_commit": source_commit,
            "source_tree": source_tree,
            "git_top_level": repo.as_posix(),
            "active_git_dir": (repo / ".git").as_posix(),
            "common_git_dir": (repo / ".git").as_posix(),
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": source_commit,
            "tree": source_tree,
            "candidate_sequence": None,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "candidate_sequence": None,
        },
        "workspace": {
            "path": None,
            "effective_cwd": None,
            "materialization": "worktree_checkout",
            "writable": True,
            "lineage_producer": True,
            "retention": "retained",
            "retained_reason": None,
            "project_root_relative_path": ".",
            "reuse_generation": 1,
        },
        "execution": {
            "workspace_path": None,
            "checkout_root": None,
            "effective_cwd": None,
        },
        "process_drain": {"status": "confirmed"},
        "result": {
            "candidate_commit": result_commit,
            "result_commit": result_commit,
            "candidate_tree": result_tree,
            "result_tree": result_tree,
            "changed_path_count": 1,
        },
        "refs": {"candidate": candidate_ref, "result": result_ref},
        "bundle": {
            "path": bundle_path.relative_to(state_path.parent.parent).as_posix(),
            "sha256": bundle_sha256,
            "size_bytes": bundle_path.stat().st_size,
            "verified": True,
        },
        "ref_publication": {
            "phase": "published",
            "repository_id": "repo",
            "run_id": "run",
            "run_key_name": "demo-run",
            "node_id": node_id,
            "task_id": task_id,
            "role": "executor",
            "round_num": round_num,
            "audit_round_num": audit_round_num,
            "destinations": {
                "candidate": {
                    "name": candidate_ref,
                    "target_oid": result_commit,
                    "expected_old_oid": None,
                },
                "result": {
                    "name": result_ref,
                    "target_oid": result_commit,
                    "expected_old_oid": None,
                },
            },
        },
    }
    payload.update(overrides)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return result_ref


def _plan(root: Path, content_ref: str | None = None) -> PreflightExecutionPlan:
    static_content_ref = content_ref or _static_content_ref(b"file")
    upstream = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(
            stage_path="input-stage",
            output_path="compiled-input.md",
            log_path="input-stage/logs",
            result_path="compiled-input.md",
        ),
        execution_policy=ExecutionPolicy(),
        input_content_ref="static-files/input.txt",
    )
    node = PreflightExecutionNode(
        id="build",
        mode="sequential",
        dependencies=["input"],
        render_plan_id="build",
        artifact_contract=ArtifactContract(
            stage_path="build-stage",
            output_path="build-result.md",
            log_path="build-stage/logs",
            result_path="build-result.md",
        ),
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
        plan_schema_version=SCHEMA_VERSION,
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
                node_id="build",
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
        dependency_graph=[
            DependencyEdge(
                source_node="input",
                target_node="build",
                artifact_name="output",
                dependency_signature=dependency_signature(
                    "input",
                    "build",
                    "output",
                ),
                target_locator="input.output",
                artifact_key="output",
            )
        ],
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


def test_assemble_prompt_rejects_symlinked_static_bundle(
    tmp_path: Path,
) -> None:
    context_root = tmp_path / "execution-stages" / "demo-run"
    content_ref = _static_content_ref(b"outside")
    static_path = context_root / "preflight" / content_ref
    static_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        static_path.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    store = _ArtifactStore(tmp_path)
    (store.results_dir / "compiled-input.md").write_text("node", encoding="utf-8")
    secrets = SecretContext()
    secrets.put("env:API_TOKEN", "secret")

    with pytest.raises(ValueError, match="missing or unsafe"):
        assemble_prompt(
            _plan(context_root, content_ref),
            _plan(context_root, content_ref).nodes[1],
            ProviderRole.EXECUTOR,
            store,
            secrets,
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
    store = _ArtifactStore(tmp_path)
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
        plan = _plan(context_root).model_copy(
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
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    source_commit = _git(repo, "rev-parse", "HEAD^{commit}")
    source_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    (repo / "future.md").write_text("dynamic\n", encoding="utf-8")
    _git(repo, "add", "future.md")
    _git(repo, "commit", "-m", "candidate")
    result_commit = _git(repo, "rev-parse", "HEAD^{commit}")
    result_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = _ArtifactStore(tmp_path)
    upstream_stage = store.stages_dir / "input-stage"
    _write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=upstream_stage,
        source_commit=source_commit,
        source_tree=source_tree,
        result_commit=result_commit,
        result_tree=result_tree,
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
                run_base_commit=source_commit,
                source_tree=source_tree,
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

    missing_file_commit = _git(
        repo,
        "commit-tree",
        source_tree,
        "-p",
        source_commit,
        "-m",
        "candidate without file",
    )
    missing_file_tree = source_tree
    _write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=upstream_stage,
        source_commit=source_commit,
        source_tree=source_tree,
        result_commit=missing_file_commit,
        result_tree=missing_file_tree,
        bundle_name="input-missing-file.bundle",
        extra_payload={"node_id": "input"},
    )

    with pytest.raises(
        NodeExecutionError,
        match="Runtime-dynamic workspace file locator does not resolve exactly",
    ):
        assemble_prompt(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )


def test_assemble_prompt_imports_bundle_for_runtime_dynamic_workspace_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    upstream_stage = store.stages_dir / "input-stage"
    result_ref = _write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=upstream_stage,
        source_commit=parent_commit,
        source_tree=parent_tree,
        result_commit=result_commit,
        result_tree=result_tree,
        bundle_name="input.bundle",
        extra_payload={"node_id": "input"},
    )
    _git(repo, "update-ref", "-d", result_ref)
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now")
    if _git_commit_exists(repo, result_commit):
        pytest.skip("git retained the test commit after pruning")
    producer_state_path = upstream_stage / "workspace-state.json"
    producer_state_before = producer_state_path.read_bytes()

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
    original_cat_blob = workspace_file_resolution.git_cat_blob

    def collect_after_prune(repo_root: str, object_id: str) -> bytes:
        imported_refs = _git(
            repo,
            "for-each-ref",
            "--format=%(objectname)",
            f"refs/crewplane/runs/{plan.run_key_name}/imports",
        ).splitlines()
        assert result_commit in imported_refs
        _git(repo, "reflog", "expire", "--expire=now", "--all")
        _git(repo, "gc", "--prune=now")
        return original_cat_blob(repo_root, object_id)

    monkeypatch.setattr(
        workspace_file_resolution,
        "git_cat_blob",
        collect_after_prune,
    )

    prompt = assemble_prompt(
        plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
    )

    assert prompt == "bundled dynamic\n"
    assert _git_commit_exists(repo, result_commit)
    assert producer_state_path.read_bytes() == producer_state_before
    assert not tuple(store.logs_dir.glob("workspace-temporary-refs-*.json"))


def test_initial_pre_review_reads_project_initial_runtime_dynamic_workspace_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Crewplane Test")
    _git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("project initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    run_base_commit = _git(repo, "rev-parse", "HEAD^{commit}")
    source_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = _ArtifactStore(tmp_path)
    locator = _reviewer_runtime_dynamic_locator(repo)
    plan = _reviewer_workspace_plan(
        context_root,
        repo,
        run_base_commit,
        source_tree,
        locator,
    )
    context = WorkspaceCandidateSourceContext(
        role_label=ProviderRole.REVIEWER,
        round_num=0,
        audit_round_num=None,
        phase="initial_pre_review",
    )

    prompt = assemble_prompt(
        plan,
        plan.nodes[1],
        ProviderRole.REVIEWER,
        store,
        SecretContext(),
        workspace_candidate_context=context,
    )
    resolved = resolve_workspace_file(
        plan,
        store,
        locator.locator_id,
        workspace_candidate_context=context,
    )

    assert prompt == "project initial\n"
    assert resolved.source_ref is not None
    assert resolved.source_ref.source_kind == "project"


def test_initial_pre_review_reads_upstream_runtime_dynamic_workspace_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Crewplane Test")
    _git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("project initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    run_base_commit = _git(repo, "rev-parse", "HEAD^{commit}")
    source_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    (repo / "README.md").write_text("upstream lineage\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "upstream")
    upstream_commit = _git(repo, "rev-parse", "HEAD^{commit}")
    upstream_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = _ArtifactStore(tmp_path)
    _write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=store.stages_dir / "input-stage",
        source_commit=run_base_commit,
        source_tree=source_tree,
        result_commit=upstream_commit,
        result_tree=upstream_tree,
        bundle_name="input.bundle",
        extra_payload={"node_id": "input"},
    )
    locator = _reviewer_runtime_dynamic_locator(repo)
    plan = _reviewer_workspace_plan(
        context_root,
        repo,
        run_base_commit,
        source_tree,
        locator,
        source_kind="node",
        source_node_id="input",
    )

    prompt = assemble_prompt(
        plan,
        plan.nodes[1],
        ProviderRole.REVIEWER,
        store,
        SecretContext(),
        workspace_candidate_context=WorkspaceCandidateSourceContext(
            role_label=ProviderRole.REVIEWER,
            round_num=0,
            audit_round_num=None,
            phase="initial_pre_review",
        ),
    )

    assert prompt == "upstream lineage\n"


def test_normal_candidate_review_rejects_missing_runtime_dynamic_candidate(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Crewplane Test")
    _git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("project initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    run_base_commit = _git(repo, "rev-parse", "HEAD^{commit}")
    source_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    context_root = tmp_path / "execution-stages" / "demo-run"
    store = _ArtifactStore(tmp_path)
    locator = _reviewer_runtime_dynamic_locator(repo)
    plan = _reviewer_workspace_plan(
        context_root,
        repo,
        run_base_commit,
        source_tree,
        locator,
    )

    with pytest.raises(RuntimeError, match="no matching executor state"):
        assemble_prompt(
            plan,
            plan.nodes[1],
            ProviderRole.REVIEWER,
            store,
            SecretContext(),
            workspace_candidate_context=WorkspaceCandidateSourceContext(
                role_label=ProviderRole.REVIEWER,
                round_num=1,
                audit_round_num=None,
            ),
        )


def test_assemble_prompt_reads_after_candidate_workspace_locator_from_candidate(
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
    source_commit = _git(repo, "rev-parse", "HEAD^{commit}")
    source_tree = _git(repo, "rev-parse", "HEAD^{tree}")
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
    build_stage = store.stages_dir / "build-stage"
    _write_lineage_state_with_bundle(
        repo=repo,
        stage_dir=build_stage,
        source_commit=source_commit,
        source_tree=source_tree,
        result_commit=result_commit,
        result_tree=result_tree,
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
        source_class="project_initial_then_candidate",
        raw_token="{{file:future.md}}",
        raw_path="future.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="future.md",
        workspace_relative_path="future.md",
        git_blob="a" * 40,
        git_file_mode="100644",
        byte_size=len("base\n"),
        canonical_blob_sha256=hashlib.sha256(b"base\n").hexdigest(),
        literal_path_verified=True,
        utf8_validated=True,
    )
    plan = _plan(context_root).model_copy(
        update={
            "workspace_source": WorkspaceSourceSnapshot(
                worktree_contract=WORKTREE_CONTRACT,
                run_base_commit=source_commit,
                source_tree=source_tree,
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
                                        "locator_id": "workspace-file-after-candidate",
                                        "source_class": "project_initial_then_candidate",
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


def _reviewer_runtime_dynamic_locator(repo: Path) -> WorkspaceFileLocator:
    return WorkspaceFileLocator(
        locator_id="workspace-file-reviewer-dynamic",
        occurrence_id="build:reviewer:0:file:README.md",
        node_id="build",
        target="reviewer_prompt",
        source_class="runtime_dynamic",
        raw_token="{{file:README.md}}",
        raw_path="README.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="README.md",
        workspace_relative_path="README.md",
    )


def _reviewer_workspace_plan(
    context_root: Path,
    repo: Path,
    run_base_commit: str,
    source_tree: str,
    locator: WorkspaceFileLocator,
    source_kind: str = "project",
    source_node_id: str | None = None,
) -> PreflightExecutionPlan:
    base_plan = _plan(context_root)
    return base_plan.model_copy(
        update={
            "workspace_source": WorkspaceSourceSnapshot(
                worktree_contract=WORKTREE_CONTRACT,
                run_base_commit=run_base_commit,
                source_tree=source_tree,
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
                base_plan.nodes[0],
                base_plan.nodes[1].model_copy(
                    update={
                        "workspace_policy": workspace_selection_record(
                            enabled=True,
                            kind="worktree",
                            source_kind=source_kind,
                            source_node_id=source_node_id,
                            clean_start="strict",
                            materialization="worktree_checkout",
                        )
                    }
                ),
            ],
            "render_plans": [
                RenderPlan(
                    render_plan_id="build",
                    node_id="build",
                    streams=[
                        RenderStream(
                            target_role=ProviderRole.REVIEWER,
                            fragments=[
                                Fragment(
                                    fragment_index=0,
                                    kind="workspace_file_locator",
                                    source_role=PromptSegmentRole.REVIEWER,
                                    locator={
                                        "locator_id": locator.locator_id,
                                        "source_class": "runtime_dynamic",
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


class _VerifiedArtifactStore(_ArtifactStore):
    def read_verified_node_artifact(
        self, request: NodeArtifactRequest, kind: str
    ) -> VerifiedNodeArtifact:
        return read_verified_node_artifact(
            self.stages_dir, self.results_dir, request, kind
        )


@pytest.mark.parametrize(
    "artifact_name",
    [
        "output",
        "output_path",
        "output_size",
        "output_sha256",
        "findings",
        "findings_path",
        "findings_size",
        "findings_sha256",
    ],
)
def test_runtime_token_verifies_backing_artifact(
    tmp_path: Path, artifact_name: str
) -> None:
    store = _VerifiedArtifactStore(tmp_path)
    plan = _plan(tmp_path)
    plan.nodes[0].artifact_contract.findings_path = "input-findings.md"
    plan.render_plans[0].streams[0].fragments = [
        Fragment(
            fragment_index=0,
            kind="runtime_locator_lookup",
            source_role=PromptSegmentRole.SHARED,
            locator={"node_id": "input", "artifact_name": artifact_name},
        )
    ]
    descriptors = [
        write_result(store.results_dir, "compiled-input.md", "output bytes"),
        write_result(store.results_dir, "input-findings.md", "findings bytes"),
    ]
    manifest = make_run_manifest("run", "demo-run")
    state_path = write_node_state(
        store.stages_dir, make_node_state(manifest, "input", descriptors)
    )
    descriptor = descriptors[artifact_name.startswith("findings")]
    path = store.results_dir / descriptor.relative_path
    original = path.read_bytes()
    expected = {
        "path": path.as_posix(),
        "size": str(len(original)),
        "sha256": hashlib.sha256(original).hexdigest(),
    }.get(artifact_name.partition("_")[2], original.decode())

    assert (
        assemble_prompt(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )
        == expected
    )
    path.write_bytes(b"x" * len(original))
    with pytest.raises(ValueError, match="artifact bytes do not match state"):
        assemble_prompt(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )
    path.unlink()
    with pytest.raises(ValueError, match="artifact is unavailable"):
        assemble_prompt(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )
    path.write_bytes(original)
    state_path.unlink()
    with pytest.raises(ValueError, match="no valid successful state descriptor"):
        assemble_prompt(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )

    if artifact_name.startswith("findings"):
        plan.nodes[0].artifact_contract.findings_path = None
        with pytest.raises(ValueError, match="has no findings artifact locator"):
            assemble_prompt(
                plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
            )


@pytest.mark.parametrize(
    "artifact_name", ["unknown", "output_unknown", "findings_unknown"]
)
def test_runtime_rejects_unsupported_artifact_tokens(
    tmp_path: Path, artifact_name: str
) -> None:
    plan = _plan(tmp_path)
    plan.render_plans[0].streams[0].fragments = [
        Fragment(
            fragment_index=0,
            kind="runtime_locator_lookup",
            source_role=PromptSegmentRole.SHARED,
            locator={"node_id": "input", "artifact_name": artifact_name},
        )
    ]
    with pytest.raises(ValueError, match="Unsupported artifact locator"):
        assemble_prompt(
            plan,
            plan.nodes[1],
            ProviderRole.EXECUTOR,
            _ArtifactStore(tmp_path),
            SecretContext(),
        )
