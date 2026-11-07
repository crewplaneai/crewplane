from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from threading import Event, get_ident

import pytest

from crewplane.artifacts.memory import imports
from crewplane.artifacts.memory.receipts import extraction_attempt, publish_receipt
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.core.memory.candidates import LessonProposal, parse_candidates
from crewplane.core.memory.models import digest_payload
from crewplane.core.memory.packets import EvidencePacket
from scripts.validation_policy import load_policy
from tests.helpers.memory import evidence_receipt, lesson_document

TIMEOUTS = load_policy().timeouts


@pytest.fixture
def packet() -> EvidencePacket:
    evidence = evidence_receipt().model_copy(
        update={"qualifications": ("Shared qualification.", "Evidence qualification.")}
    )
    identity = {
        "parent_run_id": "run",
        "parent_run_key": "parent-run",
        "workflow_signature": "0" * 64,
        "parent_outcome": "failed",
        "qualifications": ("Parent failed.", "Shared qualification."),
        "evidence": [evidence.semantic_payload()],
        "omissions": (),
    }
    return EvidencePacket.model_validate(
        {
            **identity,
            "evidence": [evidence.model_dump(mode="json")],
            "captured_at": "2026-01-02T00:00:00Z",
            "packet_digest": digest_payload(identity),
        }
    )


def candidate_markdown(evidence_ids: tuple[str, ...] = ("e-source",)) -> str:
    document = lesson_document(
        LessonProposal(title="First", body="First lesson.", evidence_ids=evidence_ids),
        LessonProposal(
            title="Second", body="Second lesson.", evidence_ids=evidence_ids
        ),
    )
    return f"```crewplane-memory-candidates\n{document.model_dump_json()}\n```\n"


def test_import_orders_receipts_around_commit_and_reuses_identities(
    tmp_path: Path, packet: EvidencePacket, monkeypatch
):
    stages = tmp_path / "parent"
    store = MemoryStore(tmp_path)
    observed = []

    def publish(stages_dir, filename, payload):
        observed.append(
            (payload["imported"], len(json.loads(store.path.read_bytes())["records"]))
        )
        return publish_receipt(stages_dir, filename, payload)

    monkeypatch.setattr(imports, "publish_receipt", publish)

    async def scenario():
        await store.initialize()
        async with extraction_attempt(stages):
            identities = await imports.import_extraction(
                tmp_path, stages, packet, candidate_markdown(), "extraction-run"
            )
        snapshot = await store.snapshot()
        assert [record.revisions[0].title for record in snapshot.records] == [
            "First",
            "Second",
        ]
        assert all(record.accepted_revision is None for record in snapshot.records)
        retained = snapshot.records[0].revisions[0].evidence[0]
        assert retained.qualifications == (
            "Parent failed.",
            "Shared qualification.",
            "Evidence qualification.",
        )
        assert retained.provenance[-1] == (
            f"Host packet {packet.packet_digest}; parent parent-run; parent outcome: failed."
        )
        assert observed == [(None, 0), ([asdict(item) for item in identities], 2)]
        receipt = json.loads((stages / "manifests/memory/candidates.json").read_bytes())
        assert receipt["imported"] == [asdict(item) for item in identities]
        assert receipt["document"] == parse_candidates(candidate_markdown()).model_dump(
            mode="json"
        )
        assert receipt["proposal_identity"] == digest_payload(
            {
                "packet": packet.packet_digest,
                "document": receipt["document"],
                "extraction_run_id": "extraction-run",
            }
        )
        assert receipt["provenance"] == {
            "author": "extractor",
            "source_identity": receipt["proposal_identity"],
            "run_id": "run",
            "extraction_run_id": "extraction-run",
        }
        async with extraction_attempt(stages):
            repeated = await imports.import_extraction(
                tmp_path, stages, packet, candidate_markdown(), "extraction-run"
            )
        assert repeated == identities
        assert await store.snapshot() == snapshot

    asyncio.run(scenario())


def test_import_requires_explicit_store_initialization(
    tmp_path: Path, packet: EvidencePacket
):
    stages = tmp_path / "parent"

    async def scenario():
        async with extraction_attempt(stages):
            with pytest.raises(ValueError, match="init"):
                await imports.import_extraction(
                    tmp_path, stages, packet, candidate_markdown(), "extraction-run"
                )
        receipt = json.loads((stages / "manifests/memory/candidates.json").read_bytes())
        assert receipt["imported"] is None
        assert not MemoryStore(tmp_path).directory.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("markdown", "message"),
    [
        (
            "not a candidate block",
            "Expected exactly one fenced crewplane-memory-candidates JSON block.",
        ),
        (
            candidate_markdown(()),
            "Extracted lessons must cite at least one host evidence ID; no useful lesson may instead return an empty lessons list.",
        ),
        (
            candidate_markdown(("unknown",)),
            "Extractor cited unknown host evidence IDs.",
        ),
        (
            "```crewplane-memory-candidates\n"
            + lesson_document(
                LessonProposal(
                    title="First", body="Unknown.", evidence_ids=("unknown",)
                ),
                LessonProposal(title="Second", body="Missing."),
            ).model_dump_json()
            + "\n```",
            "Extractor cited unknown host evidence IDs.",
        ),
    ],
    ids=["malformed", "missing-evidence", "unknown-evidence", "first-invalid-lesson"],
)
def test_invalid_candidates_fail_before_receipt_or_store_mutation(
    tmp_path: Path, packet: EvidencePacket, markdown: str, message: str
):
    stages = tmp_path / "parent"

    async def scenario():
        store = MemoryStore(tmp_path)
        initial = await store.initialize()
        async with extraction_attempt(stages):
            with pytest.raises(ValueError) as captured:
                await imports.import_extraction(
                    tmp_path,
                    stages,
                    packet,
                    markdown,
                    "extraction-run",
                )
        assert str(captured.value) == message
        assert not (stages / "manifests/memory/candidates.json").exists()
        assert await store.snapshot() == initial

    asyncio.run(scenario())


def test_empty_document_publishes_receipt_without_changing_store(
    tmp_path: Path, packet: EvidencePacket
):
    stages = tmp_path / "parent"
    document = lesson_document()
    markdown = f"```crewplane-memory-candidates\n{document.model_dump_json()}\n```"

    async def scenario():
        store = MemoryStore(tmp_path)
        initial = await store.initialize()
        original = store.path.read_bytes()
        async with extraction_attempt(stages):
            identities = await imports.import_extraction(
                tmp_path, stages, packet, markdown, "extraction-run"
            )
        assert identities == ()
        assert await store.snapshot() == initial
        assert store.path.read_bytes() == original
        receipt = json.loads((stages / "manifests/memory/candidates.json").read_bytes())
        assert receipt["document"] == document.model_dump(mode="json")
        assert receipt["imported"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("failed_publication", [1, 2])
def test_receipt_failure_preserves_commit_boundary_and_allows_retry(
    tmp_path: Path, packet: EvidencePacket, monkeypatch, failed_publication: int
):
    stages = tmp_path / "parent"
    error = OSError("receipt sync failed")
    publications = 0

    def publish(stages_dir, filename, payload):
        nonlocal publications
        publications += 1
        if publications == failed_publication:
            raise error
        return publish_receipt(stages_dir, filename, payload)

    monkeypatch.setattr(imports, "publish_receipt", publish)

    async def scenario():
        store = MemoryStore(tmp_path)
        await store.initialize()
        async with extraction_attempt(stages):
            with pytest.raises(OSError) as captured:
                await imports.import_extraction(
                    tmp_path, stages, packet, candidate_markdown(), "extraction-run"
                )
        assert captured.value is error
        assert publications == failed_publication
        snapshot = await store.snapshot()
        assert len(snapshot.records) == (0 if failed_publication == 1 else 2)
        receipt = stages / "manifests/memory/candidates.json"
        if failed_publication == 1:
            assert not receipt.exists()
        else:
            assert json.loads(receipt.read_bytes())["imported"] is None
        monkeypatch.setattr(imports, "publish_receipt", publish_receipt)
        async with extraction_attempt(stages):
            identities = await imports.import_extraction(
                tmp_path, stages, packet, candidate_markdown(), "extraction-run"
            )
        assert len(identities) == 2
        assert len((await store.snapshot()).records) == 2
        if failed_publication == 2:
            assert await store.snapshot() == snapshot
            assert [item.revision_id for item in identities] == [
                record.revisions[0].revision_id for record in snapshot.records
            ]

    asyncio.run(scenario())


@pytest.mark.race
@pytest.mark.parametrize("blocked_publication", [1, 2])
@pytest.mark.parametrize("storage_failure", [False, True])
def test_cancelled_publication_drains_before_releasing_extraction_lock(
    tmp_path: Path,
    packet: EvidencePacket,
    monkeypatch,
    blocked_publication: int,
    storage_failure: bool,
):
    stages = tmp_path / "parent"
    release = Event()
    completed = Event()
    failure = OSError("receipt publication failed")
    publications = 0
    context = ContextVar("receipt-owner", default="missing")

    async def scenario():
        context.set("extraction owner")
        store = MemoryStore(tmp_path)
        await store.initialize()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        owner_thread = get_ident()
        owners = []

        def publish(stages_dir, filename, payload):
            nonlocal publications
            publications += 1
            if publications != blocked_publication:
                return publish_receipt(stages_dir, filename, payload)
            loop.call_soon_threadsafe(entered.set)
            assert get_ident() != owner_thread, "Receipt I/O blocked the event loop"
            assert context.get() == "extraction owner"
            try:
                assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
                if storage_failure:
                    raise failure
                return publish_receipt(stages_dir, filename, payload)
            finally:
                completed.set()

        monkeypatch.setattr(imports, "publish_receipt", publish)

        async def owner():
            async with extraction_attempt(stages) as attempt:
                owners.append(attempt)
                return await imports.import_extraction(
                    tmp_path, stages, packet, candidate_markdown(), "extraction-run"
                )

        task = asyncio.create_task(owner())
        try:
            async with asyncio.timeout(TIMEOUTS.scenario_seconds) as watchdog:
                await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
                assert not task.done()
                assert len((await store.snapshot()).records) == (
                    0 if blocked_publication == 1 else 2
                )
                for message in ("first cancellation", "second cancellation"):
                    task.cancel(message)
                    await asyncio.sleep(0)
                    assert not task.done()
                    assert owners[0].active
                    with pytest.raises(TimeoutError):
                        async with extraction_attempt(stages):
                            pytest.fail("publication lost its extraction lock")
                release.set()
                with pytest.raises(asyncio.CancelledError) as captured:
                    await task
                if storage_failure:
                    assert captured.value.__cause__ is failure
            assert not watchdog.expired()
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), TIMEOUTS.cleanup_seconds
            )
        assert completed.is_set()
        assert not owners[0].active
        assert publications == blocked_publication
        async with extraction_attempt(stages):
            snapshot = await store.snapshot()
            assert len(snapshot.records) == (0 if blocked_publication == 1 else 2)
            receipt = stages / "manifests/memory/candidates.json"
            if storage_failure and blocked_publication == 1:
                assert not receipt.exists()
            else:
                imported = json.loads(receipt.read_bytes())["imported"]
                if storage_failure or blocked_publication == 1:
                    assert imported is None
                else:
                    assert [item["record_id"] for item in imported] == [
                        record.record_id for record in snapshot.records
                    ]

    asyncio.run(scenario())


@pytest.mark.race
@pytest.mark.parametrize("blocked_publication", [1, 2])
@pytest.mark.parametrize("storage_failure", [False, True])
def test_runner_shutdown_finishes_receipt_before_owner_cleanup(
    tmp_path: Path,
    packet: EvidencePacket,
    monkeypatch,
    blocked_publication: int,
    storage_failure: bool,
):
    stages = tmp_path / "parent"
    release = Event()
    completed = Event()
    failure = OSError("receipt publication failed during shutdown")
    publications = 0
    cleanup = []
    errors = []

    async def main():
        await MemoryStore(tmp_path).initialize()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def publish(stages_dir, filename, payload):
            nonlocal publications
            publications += 1
            if publications != blocked_publication:
                return publish_receipt(stages_dir, filename, payload)
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
                if storage_failure:
                    raise failure
                return publish_receipt(stages_dir, filename, payload)
            finally:
                completed.set()

        monkeypatch.setattr(imports, "publish_receipt", publish)

        async def owner():
            async with extraction_attempt(stages):
                try:
                    await imports.import_extraction(
                        tmp_path, stages, packet, candidate_markdown(), "extraction-run"
                    )
                except (OSError, asyncio.CancelledError) as exc:
                    errors.append(exc)
                finally:
                    cleanup.append(completed.is_set())

        async def release_on_shutdown():
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                release.set()

        tasks = [
            asyncio.create_task(owner()),
            asyncio.create_task(release_on_shutdown()),
        ]
        await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
        assert all(not task.done() for task in tasks)

    try:
        asyncio.run(main())
    finally:
        release.set()
    assert cleanup == [True]
    assert len(errors) == 1
    assert isinstance(errors[0], asyncio.CancelledError)
    if storage_failure:
        assert errors[0].__cause__ is failure

    async def reacquire():
        async with extraction_attempt(stages):
            assert publications == blocked_publication

    asyncio.run(reacquire())
