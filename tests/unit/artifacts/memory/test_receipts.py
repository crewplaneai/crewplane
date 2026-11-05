from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.memory.completion import read_completion
from crewplane.artifacts.memory.receipts import (
    ExtractionAttempt,
    ExtractionReceipt,
    extraction_attempt,
    read_extraction,
    read_packet,
    save_extraction,
)
from tests.helpers.memory_runtime import setup_parent


@pytest.mark.parametrize("field", ["schema_version", "omissions", "provenance"])
def test_saved_packet_missing_current_fields_is_not_default_filled(
    tmp_path: Path, field
):
    _config, parent, _packet, _fixtures, _console = setup_parent(tmp_path)
    path = parent.stages_dir / "manifests/memory/evidence.json"
    payload = json.loads(path.read_bytes())
    if field == "provenance":
        payload["evidence"][0].pop(field)
    else:
        payload.pop(field)
    atomic_write_json(path, payload)
    with pytest.raises(ValueError):
        read_packet(parent.stages_dir)


@pytest.mark.parametrize(
    "field",
    [
        "validation_status",
        "manifest_committed",
        "cleanup_confirmed",
        "lock_released",
        "result_displayed",
    ],
)
def test_saved_extraction_missing_current_field_is_not_default_filled(
    tmp_path: Path, field
):
    _config, parent, packet, _fixtures, _console = setup_parent(tmp_path)
    receipt = ExtractionReceipt(
        packet_digest=packet.packet_digest, provider_status="not_started"
    )

    async def publish():
        async with extraction_attempt(parent.stages_dir) as attempt:
            save_extraction(attempt, receipt)

    asyncio.run(publish())
    path = parent.stages_dir / "manifests/memory/extraction.json"
    payload = json.loads(path.read_bytes())
    payload.pop(field)
    atomic_write_json(path, payload)
    with pytest.raises(ValueError, match="current shape"):
        read_extraction(parent.stages_dir)


@pytest.mark.parametrize(
    "field",
    ["manifest_committed", "cleanup_confirmed", "lock_released", "result_displayed"],
)
def test_extraction_completion_facts_require_actual_booleans(field):
    with pytest.raises(ValueError):
        ExtractionReceipt.model_validate(
            {"packet_digest": "0" * 64, "provider_status": "succeeded", field: 1}
        )


@pytest.mark.parametrize(
    "change",
    [{"schema_version": "future"}, {"cleanup_confirmed": 1}, {"unknown": True}],
)
def test_completion_receipt_rejects_unsupported_or_malformed_proof(
    tmp_path: Path, change
):
    _config, parent, _packet, _fixtures, _console = setup_parent(tmp_path)
    path = parent.stages_dir / "manifests/completion.json"
    payload = json.loads(path.read_bytes())
    payload.update(change)
    atomic_write_json(path, payload)
    with pytest.raises(ValueError):
        read_completion(parent.stages_dir)


def test_extraction_receipt_requires_live_owner_and_preserves_stable_lock(
    tmp_path: Path,
):
    _config, parent, packet, _fixtures, _console = setup_parent(tmp_path)
    receipt = ExtractionReceipt(
        packet_digest=packet.packet_digest, provider_status="not_started"
    )
    with pytest.raises(ValueError, match="owning attempt"):
        save_extraction(ExtractionAttempt(parent.stages_dir), receipt)

    async def publish():
        async with extraction_attempt(parent.stages_dir) as owner:
            save_extraction(owner, receipt)
            path = parent.stages_dir / "manifests/memory/extraction.lock"
            inode = path.stat().st_ino
            with pytest.raises(TimeoutError):
                async with extraction_attempt(parent.stages_dir):
                    pytest.fail("a non-owning attempt acquired the parent receipt lock")
            assert read_extraction(parent.stages_dir) == receipt
        with pytest.raises(ValueError, match="owning attempt"):
            save_extraction(owner, receipt)
        async with extraction_attempt(parent.stages_dir) as next_owner:
            assert path.stat().st_ino == inode
            save_extraction(next_owner, receipt)

    asyncio.run(publish())
