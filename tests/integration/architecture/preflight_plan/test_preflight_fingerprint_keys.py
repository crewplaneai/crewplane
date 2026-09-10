from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.console import Console

import crewplane.core.preflight.secrets as preflight_secrets
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.preflight import (
    FingerprintKeyCache,
    FingerprintKeyProvider,
    PreflightCompileOptions,
    compile_preflight_preview,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)

from .helpers import make_source, mock_config


def test_absent_read_only_fingerprint_key_is_run_scoped_and_artifact_free(
    tmp_path: Path,
) -> None:
    config = mock_config()
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.EXECUTOR, content="{{env:API_TOKEN}}"
                    )
                ],
            )
        ],
    )
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )

    def compile_once(options: PreflightCompileOptions) -> str:
        preview = compile_preflight_preview(
            source=make_source(workflow),
            config=config,
            runtime_snapshot=snapshot.snapshot,
            options=options,
        )
        assert not preview.diagnostics
        assert preview.workflow_signature is not None
        assert preview.fingerprint_metadata["fingerprint_key_persisted"] is False
        assert preview.fingerprint_metadata["persisted_key_path"] is None
        return preview.workflow_signature

    first_run_options = PreflightCompileOptions(
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        environment={"API_TOKEN": "super-secret"},
        fingerprint_key_policy="read_only",
    )
    second_run_options = PreflightCompileOptions(
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        environment={"API_TOKEN": "super-secret"},
        fingerprint_key_policy="read_only",
    )

    assert compile_once(first_run_options) == compile_once(first_run_options)
    assert compile_once(first_run_options) != compile_once(second_run_options)
    assert not (tmp_path / ".crewplane" / "preflight" / "fingerprint.key").exists()


def test_ephemeral_fingerprint_key_cache_is_explicitly_scoped(tmp_path: Path) -> None:
    state_dir = tmp_path / ".crewplane"
    cache = FingerprintKeyCache()

    first = FingerprintKeyProvider(state_dir, cache=cache).load_key("ephemeral")
    second = FingerprintKeyProvider(state_dir, cache=cache).load_key("ephemeral")
    independent = FingerprintKeyProvider(
        state_dir,
        cache=FingerprintKeyCache(),
    ).load_key("ephemeral")

    assert first.key == second.key
    assert first.key != independent.key
    assert not (state_dir / "preflight" / "fingerprint.key").exists()


def test_concurrent_first_fingerprint_key_publish_converges(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker_count = 4
    start_barrier = threading.Barrier(worker_count)
    publish_barrier = threading.Barrier(worker_count)
    original_token_bytes = preflight_secrets.secrets.token_bytes

    def synchronized_token_bytes(size: int) -> bytes:
        publish_barrier.wait(timeout=5)
        return original_token_bytes(size)

    monkeypatch.setattr(
        preflight_secrets.secrets,
        "token_bytes",
        synchronized_token_bytes,
    )

    def load_key() -> bytes:
        start_barrier.wait(timeout=5)
        result = FingerprintKeyProvider(tmp_path / ".crewplane").load_key(
            "persist_if_needed"
        )
        assert not result.diagnostics
        assert result.persisted
        return result.key

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(load_key) for _ in range(worker_count)]
        keys = [future.result() for future in futures]

    key_path = tmp_path / ".crewplane" / "preflight" / "fingerprint.key"
    assert key_path.exists()
    assert key_path.stat().st_size == 32
    assert set(keys) == {key_path.read_bytes()}
    assert list(key_path.parent.glob(f".{key_path.name}.*.tmp")) == []
