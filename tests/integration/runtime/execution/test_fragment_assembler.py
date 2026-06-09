from __future__ import annotations

import hashlib
from pathlib import Path

import orchestrator_cli.core.preflight.references as preflight_references
import orchestrator_cli.core.workflow_models as workflow_models
import orchestrator_cli.core.workflow_validation as workflow_validation
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    ExecutionPolicy,
    Fragment,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    RenderPlan,
    RenderStream,
)
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.runtime.execution.fragment_assembler import assemble_prompt
from orchestrator_cli.version import SCHEMA_VERSION


class _ArtifactStore:
    run_id = "run"
    task_name = "demo"
    log_cli_output = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.stages_dir = root / "stages"
        self.results_dir = root / "results"
        self.logs_dir = root / "logs"
        self.results_dir.mkdir(parents=True)

    def get_stage_output_path(self, stage_name: str) -> Path:
        return self.results_dir / f"{stage_name}-result.md"

    def get_stage_findings_path(self, stage_name: str) -> Path:
        return self.results_dir / f"{stage_name}-findings.md"


def _static_content_ref(payload: bytes) -> str:
    return f"static-files/{hashlib.sha256(payload).hexdigest()}.txt"


def _plan(root: Path, content_ref: str | None = None) -> PreflightExecutionPlan:
    static_content_ref = content_ref or _static_content_ref(b"file")
    upstream = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(output_path="compiled-input.md"),
        execution_policy=ExecutionPolicy(),
    )
    node = PreflightExecutionNode(
        id="build",
        mode="sequential",
        render_plan_id="build",
        artifact_contract=ArtifactContract(output_path="build-result.md"),
        execution_policy=ExecutionPolicy(),
    )
    return PreflightExecutionPlan(
        run_id="run",
        run_key_name="demo-run",
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
                        target_role="executor",
                        fragments=[
                            Fragment(
                                fragment_index=0,
                                kind="literal",
                                source_role="shared",
                                text="A ",
                            ),
                            Fragment(
                                fragment_index=1,
                                kind="static_file_content",
                                source_role="shared",
                                content_ref=static_content_ref,
                            ),
                            Fragment(
                                fragment_index=2,
                                kind="literal",
                                source_role="shared",
                                text=" B ",
                            ),
                            Fragment(
                                fragment_index=3,
                                kind="runtime_locator_lookup",
                                source_role="shared",
                                locator={
                                    "node_id": "input",
                                    "artifact_name": "output",
                                },
                            ),
                            Fragment(
                                fragment_index=4,
                                kind="literal",
                                source_role="shared",
                                text=" C ",
                            ),
                            Fragment(
                                fragment_index=5,
                                kind="static_env",
                                source_role="shared",
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
    prompt = assemble_prompt(plan, plan.nodes[1], "executor", store, secrets)

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

    prompt = assemble_prompt(plan, plan.nodes[1], "executor", store, secrets)

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

    prompt = assemble_prompt(plan, plan.nodes[1], "executor", store, secrets)

    assert prompt == "A bundled B node C secret"
