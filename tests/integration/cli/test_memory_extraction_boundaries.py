from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from crewplane.artifacts.locks import SameContextLock
from crewplane.artifacts.memory.receipts import read_extraction
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.cli.memory import extraction
from crewplane.cli.run.observability import WorkflowCancelledByUser
from crewplane.core.memory.candidates import LessonProposal
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from crewplane.runtime.execution import execute_workflow
from tests.helpers.memory import lesson_document
from tests.helpers.memory_processes import line, release, worker
from tests.helpers.memory_runtime import setup_parent
from tests.helpers.platforms import requires_posix


@pytest.mark.parametrize("cancel", ["task", "user"])
def test_extraction_cancellation_propagates_even_if_receipt_publication_fails(
    tmp_path: Path, monkeypatch, cancel
):
    config, parent, packet, _fixtures, console = setup_parent(tmp_path)
    error = asyncio.CancelledError() if cancel == "task" else WorkflowCancelledByUser()
    monkeypatch.setattr(
        extraction, "execute_workflow_run", AsyncMock(side_effect=error)
    )
    original = extraction.save_extraction
    calls = 0

    def fail_terminal_receipt(stages, receipt):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("receipt storage unavailable")
        original(stages, receipt)

    monkeypatch.setattr(extraction, "save_extraction", fail_terminal_receipt)
    with pytest.raises(type(error)):
        asyncio.run(extraction.extract_memory(config, parent, console))


@pytest.mark.parametrize("receipt_failure", [False, True])
def test_unconfirmed_extraction_lock_release_remains_fatal(
    tmp_path: Path, monkeypatch, receipt_failure
):
    config, parent, packet, _fixtures, console = setup_parent(tmp_path)
    child = replace(parent, lock_released=False)
    monkeypatch.setattr(
        extraction, "execute_workflow_run", AsyncMock(return_value=child)
    )
    original = extraction.save_extraction
    calls = 0

    def save(stages, receipt):
        nonlocal calls
        calls += 1
        if receipt_failure and calls > 1:
            raise OSError("receipt storage unavailable")
        original(stages, receipt)

    monkeypatch.setattr(extraction, "save_extraction", save)
    with pytest.raises(extraction.ExtractionCleanupError):
        asyncio.run(extraction.extract_memory(config, parent, console))


def test_unconfirmed_process_cleanup_is_not_an_ordinary_learning_failure(
    tmp_path: Path, monkeypatch
):
    config, parent, packet, _fixtures, console = setup_parent(tmp_path)
    error = ProcessDrainError(
        ProcessDrainEvidence(123, 123, True, False), "not drained"
    )
    monkeypatch.setattr(
        extraction, "execute_workflow_run", AsyncMock(side_effect=error)
    )
    with pytest.raises(ProcessDrainError, match="not drained"):
        asyncio.run(extraction.extract_memory(config, parent, console))


def test_explicit_extraction_checks_store_before_any_provider_call(
    tmp_path: Path, monkeypatch
):
    config, parent, _packet, _fixtures, console = setup_parent(tmp_path)
    (tmp_path / ".crewplane/memory/store.lock").unlink()
    execute = AsyncMock()
    monkeypatch.setattr(extraction, "execute_workflow_run", execute)
    with pytest.raises(ValueError, match="lock"):
        asyncio.run(extraction.extract_memory(config, parent, console))
    execute.assert_not_called()
    assert not (parent.stages_dir / "manifests/memory/extraction.json").exists()


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("release_raises", [False, True])
def test_exceptional_child_completion_preserves_unconfirmed_own_release(
    tmp_path: Path, monkeypatch, timeout, release_raises
):
    config, parent, _packet, _fixtures, console = setup_parent(tmp_path)
    original = SameContextLock.release
    handles = []

    def refused(lock, terminal_complete=True, cleanup_unconfirmed=False):
        assert terminal_complete and not cleanup_unconfirmed
        handles.append(lock)
        if release_raises:
            raise OSError("cannot inspect lock owner")
        return False

    monkeypatch.setattr(SameContextLock, "release", refused)
    if timeout:
        config.settings.memory.extraction_timeout_seconds = 0.05
        config.settings.integrations.invoker.options["delay_seconds"] = 1
    try:
        with pytest.raises(extraction.ExtractionCleanupError, match="release"):
            asyncio.run(extraction.extract_memory(config, parent, console))
        assert len(handles) == 1
        assert handles[0].lock_dir.is_dir()
    finally:
        for handle in handles:
            assert original(handle)


@pytest.mark.parametrize("release_before_retry", [False, True])
@pytest.mark.parametrize("regenerate", [False, True])
def test_successful_extraction_cannot_retry_unconfirmed_own_release(
    tmp_path: Path, monkeypatch, release_before_retry, regenerate
):
    config, parent, packet, fixtures, console = setup_parent(tmp_path)
    document = lesson_document(
        LessonProposal(
            title="Protected reads",
            body="Preserve protected source reads.",
            evidence_ids=(packet.evidence[0].evidence_id,),
        )
    )
    (fixtures / "extract.md").write_text(
        "```crewplane-memory-candidates\n" + document.model_dump_json() + "\n```",
        encoding="utf-8",
        newline="\n",
    )
    original = SameContextLock.release
    handles = []

    def refused(lock, terminal_complete=True, cleanup_unconfirmed=False):
        assert terminal_complete and not cleanup_unconfirmed
        handles.append(lock)
        return False

    monkeypatch.setattr(SameContextLock, "release", refused)
    try:
        with pytest.raises(extraction.ExtractionCleanupError, match="release"):
            asyncio.run(extraction.extract_memory(config, parent, console))
        assert len(handles) == 1
        assert handles[0].lock_dir.is_dir()
        receipt = read_extraction(parent.stages_dir)
        assert receipt.provider_status == "succeeded"
        assert receipt.lock_released is False
        assert not receipt.completion_eligible
        assert receipt.validation_status == receipt.import_status == "not_attempted"
        monkeypatch.setattr(SameContextLock, "release", original)
        if release_before_retry:
            assert original(handles[0])
        before = (parent.stages_dir / "manifests/memory/extraction.json").read_bytes()
        with pytest.raises(extraction.ExtractionCleanupError, match="release"):
            asyncio.run(extraction.extract_memory(config, parent, console, regenerate))
        assert (
            parent.stages_dir / "manifests/memory/extraction.json"
        ).read_bytes() == before
        assert not asyncio.run(MemoryStore(tmp_path).snapshot()).records
        assert len(list((parent.state_dir / "execution-stages").iterdir())) == 2
        assert handles[0].lock_dir.is_dir() is not release_before_retry
    finally:
        if handles and handles[0].lock_dir.exists():
            assert original(handles[0])


@pytest.mark.parametrize("failure", ["cleanup", "release", "release_error"])
@pytest.mark.parametrize("regenerate", [False, True])
def test_unfinished_extraction_receipt_cannot_hide_unconfirmed_child_completion(
    tmp_path: Path, monkeypatch, failure, regenerate
):
    config, parent, packet, fixtures, console = setup_parent(tmp_path)
    document = lesson_document(
        LessonProposal(
            title="Protected reads",
            body="Preserve protected source reads.",
            evidence_ids=(packet.evidence[0].evidence_id,),
        )
    )
    (fixtures / "extract.md").write_text(
        "```crewplane-memory-candidates\n" + document.model_dump_json() + "\n```",
        encoding="utf-8",
        newline="\n",
    )
    original_execute = extraction.execute_workflow_run
    original_save = extraction.save_extraction
    original_release = SameContextLock.release
    handles = []
    publications = 0
    parent_manifest = (parent.stages_dir / "manifests/run.json").read_bytes()

    async def cleanup_failed(*args, **kwargs):
        await execute_workflow(*args, **kwargs)
        raise ProcessDrainError(
            ProcessDrainEvidence(123, 123, True, False), "cleanup unconfirmed"
        )

    async def execute_with_cleanup_failure(*args, **kwargs):
        kwargs["execute_workflow_impl"] = cleanup_failed
        return await original_execute(*args, **kwargs)

    def refuse_release(lock, terminal_complete=True, cleanup_unconfirmed=False):
        assert terminal_complete and not cleanup_unconfirmed
        handles.append(lock)
        if failure == "release_error":
            raise OSError("cannot inspect lock owner")
        return False

    def fail_terminal_publication(stages, receipt):
        nonlocal publications
        publications += 1
        if publications == 2:
            raise OSError("terminal receipt unavailable before replacement")
        original_save(stages, receipt)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(extraction, "save_extraction", fail_terminal_publication)
            if failure == "cleanup":
                patch.setattr(
                    extraction, "execute_workflow_run", execute_with_cleanup_failure
                )
            else:
                patch.setattr(SameContextLock, "release", refuse_release)
            with pytest.raises(extraction.ExtractionCleanupError):
                asyncio.run(extraction.extract_memory(config, parent, console))
        assert publications == 2
        receipt = read_extraction(parent.stages_dir)
        assert receipt.provider_status == "not_started"
        assert receipt.extraction_run_id is receipt.extraction_run_key is None
        assert receipt.cleanup_confirmed is receipt.lock_released is None
        assert bool(list((parent.state_dir / "locks").iterdir())) is (
            failure != "cleanup"
        )
        path = parent.stages_dir / "manifests/memory/extraction.json"
        before = path.read_bytes()
        with pytest.raises(extraction.ExtractionCleanupError, match="unconfirmed"):
            asyncio.run(extraction.extract_memory(config, parent, console, regenerate))
        assert path.read_bytes() == before
        assert len(list((parent.state_dir / "execution-stages").iterdir())) == 2
        assert not asyncio.run(MemoryStore(tmp_path).snapshot()).records
        assert (
            parent.stages_dir / "manifests/run.json"
        ).read_bytes() == parent_manifest
    finally:
        for handle in handles:
            assert original_release(handle)


@pytest.mark.parametrize("regenerate", [False, True])
def test_explicitly_completed_no_child_failure_remains_retryable(
    tmp_path: Path, monkeypatch, regenerate
):
    config, parent, _packet, fixtures, console = setup_parent(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(
            "crewplane.cli.run.execution.collect_invoker_availability_diagnostics",
            AsyncMock(side_effect=OSError("preflight unavailable before child")),
        )
        first = asyncio.run(extraction.extract_memory(config, parent, console))
    assert "failed" in first.message
    receipt = read_extraction(parent.stages_dir)
    assert receipt.provider_status == "failed"
    assert receipt.extraction_run_id is receipt.extraction_run_key is None
    assert receipt.cleanup_confirmed is receipt.lock_released is True
    assert len(list((parent.state_dir / "execution-stages").iterdir())) == 1
    (fixtures / "extract.md").write_text(
        "```crewplane-memory-candidates\n"
        + lesson_document().model_dump_json()
        + "\n```",
        encoding="utf-8",
        newline="\n",
    )
    result = asyncio.run(extraction.extract_memory(config, parent, console, regenerate))
    assert result.message.endswith("no useful lesson proposed.")
    assert read_extraction(parent.stages_dir).completion_eligible
    assert len(list((parent.state_dir / "execution-stages").iterdir())) == 2


@requires_posix
@pytest.mark.race
@pytest.mark.parametrize("publication_failure", [False, True])
@pytest.mark.parametrize("regenerate", [False, True])
def test_concurrent_extraction_cannot_replace_owning_attempt_cleanup_proof(
    tmp_path: Path, publication_failure, regenerate
):
    config, parent, packet, fixtures, console = setup_parent(tmp_path)
    document = lesson_document(
        LessonProposal(
            title="Protected reads",
            body="Preserve protected source reads.",
            evidence_ids=(packet.evidence[0].evidence_id,),
        )
    )
    (fixtures / "extract.md").write_text(
        "```crewplane-memory-candidates\n" + document.model_dump_json() + "\n```",
        encoding="utf-8",
        newline="\n",
    )
    request = json.dumps(
        {
            "config": config.model_dump(mode="json"),
            "parent_key": parent.plan.run_key_name,
        }
    )
    manifest = (parent.stages_dir / "manifests/run.json").read_bytes()
    mode = "extract_cleanup_publication" if publication_failure else "extract_cleanup"
    with worker(tmp_path, mode, request) as owner:
        assert line(owner) == "ready"
        release(owner)
        assert line(owner) == "read"
        with worker(tmp_path, "extract_contender", request) as contender:
            assert line(contender) == "ready"
            release(contender)
            contender_status = line(contender)
            release(owner)
            assert line(owner) == "executed"
            # Provider work holds only the parent extraction lock, not the store lock.
            assert not asyncio.run(MemoryStore(tmp_path).snapshot()).records
            if contender_status == "read":
                release(contender)
                contender_status = line(contender)
            release(owner)
            assert line(owner) == "error:ExtractionCleanupError"
            assert line(owner) == "done"
            owning_receipt = read_extraction(parent.stages_dir)
            if contender_status == "terminal_waiting":
                release(contender)
                contender_status = line(contender)
            assert line(contender) == "done"
    receipt_path = parent.stages_dir / "manifests/memory/extraction.json"
    retained = receipt_path.read_bytes()
    assert read_extraction(parent.stages_dir) == owning_receipt
    assert contender_status == "error:TimeoutError"
    assert owning_receipt.cleanup_confirmed is (None if publication_failure else False)
    assert len(list((parent.state_dir / "execution-stages").iterdir())) == 2
    with pytest.raises(extraction.ExtractionCleanupError, match="unconfirmed"):
        asyncio.run(extraction.extract_memory(config, parent, console, regenerate))
    assert receipt_path.read_bytes() == retained
    assert len(list((parent.state_dir / "execution-stages").iterdir())) == 2
    assert not asyncio.run(MemoryStore(tmp_path).snapshot()).records
    assert (parent.stages_dir / "manifests/run.json").read_bytes() == manifest
