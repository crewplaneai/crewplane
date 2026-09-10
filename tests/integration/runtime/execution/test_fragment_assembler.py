from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import crewplane.core.preflight.references as preflight_references
import crewplane.core.workflow.models as workflow_models
import crewplane.core.workflow.validation as workflow_validation
from crewplane.architecture.contracts import NodeArtifactRequest, VerifiedNodeArtifact
from crewplane.artifacts.verification import read_verified_node_artifact
from crewplane.core.preflight.models import (
    Fragment,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.fragment_assembler import assemble_prompt
from tests.helpers.resume import (
    make_node_state,
    make_run_manifest,
    write_node_state,
    write_result,
)
from tests.integration.runtime.execution.fragment_assembler_support import (
    FragmentArtifactStore,
    make_fragment_plan,
    make_static_content_ref,
)


def test_assemble_prompt_preserves_fragment_order(tmp_path: Path) -> None:
    context_root = tmp_path / "execution-stages" / "demo-run"
    content_ref = make_static_content_ref(b"file")
    static_path = context_root / "preflight" / content_ref
    static_path.parent.mkdir(parents=True)
    static_path.write_text("file", encoding="utf-8")

    store = FragmentArtifactStore(tmp_path)
    (store.results_dir / "compiled-input.md").write_text("node", encoding="utf-8")

    secrets = SecretContext()
    secrets.put("env:API_TOKEN", "secret")

    plan = make_fragment_plan(context_root)
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
    content_ref = make_static_content_ref(b"file")
    static_path = context_root / "preflight" / content_ref
    static_path.parent.mkdir(parents=True)
    static_path.write_text("file", encoding="utf-8")
    store = FragmentArtifactStore(tmp_path)
    (store.results_dir / "compiled-input.md").write_text("node", encoding="utf-8")
    secrets = SecretContext()
    secrets.put("env:API_TOKEN", "secret")
    plan = make_fragment_plan(context_root)

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
    plan = make_fragment_plan(context_root, content_ref=content_ref).model_copy(
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
    store = FragmentArtifactStore(tmp_path)
    (store.results_dir / "compiled-input.md").write_text("node", encoding="utf-8")
    secrets = SecretContext()
    secrets.put("env:API_TOKEN", "secret")

    prompt = assemble_prompt(plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets)

    assert prompt == "A bundled B node C secret"


def test_assemble_prompt_rejects_symlinked_static_bundle(
    tmp_path: Path,
) -> None:
    context_root = tmp_path / "execution-stages" / "demo-run"
    content_ref = make_static_content_ref(b"outside")
    static_path = context_root / "preflight" / content_ref
    static_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        static_path.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    store = FragmentArtifactStore(tmp_path)
    (store.results_dir / "compiled-input.md").write_text("node", encoding="utf-8")
    secrets = SecretContext()
    secrets.put("env:API_TOKEN", "secret")

    with pytest.raises(ValueError, match="missing or unsafe"):
        assemble_prompt(
            make_fragment_plan(context_root, content_ref),
            make_fragment_plan(context_root, content_ref).nodes[1],
            ProviderRole.EXECUTOR,
            store,
            secrets,
        )


class _VerifiedArtifactStore(FragmentArtifactStore):
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
    plan = make_fragment_plan(tmp_path)
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
    plan = make_fragment_plan(tmp_path)
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
            FragmentArtifactStore(tmp_path),
            SecretContext(),
        )
