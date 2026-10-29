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
from crewplane.runtime.execution.fragment_assembler import assemble_prompt_details
from tests.helpers.platforms import symlink_or_skip
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
    prompt = assemble_prompt_details(
        plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets
    ).text

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

    prompt = assemble_prompt_details(
        plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets
    ).text

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
    plan = make_fragment_plan(context_root, content_ref, payload)
    store = FragmentArtifactStore(tmp_path)
    (store.results_dir / "compiled-input.md").write_text("node", encoding="utf-8")
    secrets = SecretContext()
    secrets.put("env:API_TOKEN", "secret")

    prompt = assemble_prompt_details(
        plan, plan.nodes[1], ProviderRole.EXECUTOR, store, secrets
    ).text

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
    symlink_or_skip(static_path, outside)
    store = FragmentArtifactStore(tmp_path)
    (store.results_dir / "compiled-input.md").write_text("node", encoding="utf-8")
    secrets = SecretContext()
    secrets.put("env:API_TOKEN", "secret")

    with pytest.raises(ValueError, match="missing or unsafe"):
        assemble_prompt_details(
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
        assemble_prompt_details(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        ).text
        == expected
    )
    path.write_bytes(b"x" * len(original))
    with pytest.raises(ValueError, match="artifact bytes do not match state"):
        assemble_prompt_details(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )
    path.unlink()
    with pytest.raises(ValueError, match="artifact is unavailable"):
        assemble_prompt_details(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )
    path.write_bytes(original)
    state_path.unlink()
    with pytest.raises(ValueError, match="no valid successful state descriptor"):
        assemble_prompt_details(
            plan, plan.nodes[1], ProviderRole.EXECUTOR, store, SecretContext()
        )

    if artifact_name.startswith("findings"):
        plan.nodes[0].artifact_contract.findings_path = None
        with pytest.raises(ValueError, match="has no findings artifact locator"):
            assemble_prompt_details(
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
        assemble_prompt_details(
            plan,
            plan.nodes[1],
            ProviderRole.EXECUTOR,
            FragmentArtifactStore(tmp_path),
            SecretContext(),
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"a\nb\n",
        b"a\r\nb\r\n",
        b"a\rb\r",
        b"a\r\nb\rc\n",
        "雪\n".encode(),
        b"no newline",
        b" \t\n",
        b"",
    ],
)
@pytest.mark.parametrize("consumer", ["prompt", "input"])
def test_static_consumers_preserve_verified_bytes(tmp_path, payload, consumer):
    plan, consume, published = _static_consumer(tmp_path, payload, consumer)
    if consumer == "input" and not payload.strip():
        with pytest.raises(RuntimeError, match="empty after preflight"):
            consume()
        assert not published.exists()
    else:
        assert consume() == payload


@pytest.mark.parametrize(
    "fault",
    [
        "replacement",
        "growth",
        "truncation",
        "missing",
        "no_descriptor",
        "ambiguous",
        "encoding",
        "negative_size",
        "boolean_size",
        "sha256",
        "invalid_utf8",
        "nul",
        "symlink",
        "hardlink",
        "traversal",
        "absolute",
        "backslash",
    ],
)
@pytest.mark.parametrize("consumer", ["prompt", "input"])
def test_static_consumers_reject_untrusted_bundle(tmp_path, fault, consumer):
    from tests.helpers.static_resources import static_resource

    payload = b"verified bytes"
    plan, consume, published = _static_consumer(tmp_path, payload, consumer)
    resource = plan.static_resources[0]
    path = Path(plan.context_root) / "preflight" / resource.content_ref
    if fault in {"replacement", "growth", "truncation", "invalid_utf8", "nul"}:
        replacement = {
            "replacement": b"x" * len(payload),
            "growth": payload + b"extra",
            "truncation": payload[:-1],
            "invalid_utf8": b"\xff",
            "nul": b"\x00",
        }[fault]
        path.write_bytes(replacement)
        if fault in {"invalid_utf8", "nul"}:
            plan.static_resources = [static_resource(replacement, resource.content_ref)]
    elif fault == "missing":
        path.unlink()
    elif fault == "no_descriptor":
        plan.static_resources = []
    elif fault == "ambiguous":
        plan.static_resources.append(resource)
    elif fault in {"encoding", "negative_size", "boolean_size", "sha256"}:
        update = {
            "encoding": {"encoding": "latin1"},
            "negative_size": {"size_bytes": -1},
            "boolean_size": {"size_bytes": True},
            "sha256": {"sha256": "invalid"},
        }[fault]
        plan.static_resources = [resource.model_copy(update=update)]
    elif fault in {"symlink", "hardlink"}:
        outside = tmp_path / "outside"
        outside.write_bytes(payload)
        path.unlink()
        if fault == "symlink":
            symlink_or_skip(path, outside)
        else:
            path.hardlink_to(outside)
    else:
        reference = {
            "traversal": "../outside",
            "absolute": "/outside",
            "backslash": "static-files\\outside",
        }[fault]
        resource.content_ref = reference
        plan.nodes[0].input_content_ref = reference
        plan.render_plans[0].streams[0].fragments[0].content_ref = reference
    with pytest.raises((ValueError, RuntimeError)):
        consume()
    assert not published.exists()


def _static_consumer(tmp_path, payload, consumer):
    from crewplane.artifacts import OutputManager
    from crewplane.runtime.execution.common import CompiledRuntimeContext
    from crewplane.runtime.execution.input import execute_input_stage

    root = tmp_path / "bundle"
    content_ref = make_static_content_ref(payload)
    path = root / "preflight" / content_ref
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    plan = make_fragment_plan(root, content_ref, payload)
    plan.nodes[0].input_content_ref = content_ref
    plan.render_plans[0].streams[0].fragments = [
        Fragment(
            fragment_index=0,
            kind="static_file_content",
            source_role=PromptSegmentRole.SHARED,
            content_ref=content_ref,
        )
    ]
    output = OutputManager("workflow", base_dir=tmp_path / "artifacts")
    published = output.stages_dir / "input-stage" / "input_round1.md"

    def consume():
        if consumer == "prompt":
            return assemble_prompt_details(
                plan, plan.nodes[1], ProviderRole.EXECUTOR, output, SecretContext()
            ).text.encode("utf-8")
        execute_input_stage(
            plan.nodes[0], output, CompiledRuntimeContext(plan, SecretContext())
        )
        return published.read_bytes()

    return plan, consume, published
