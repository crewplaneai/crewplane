from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import (
    ensure_contained_directory,
    ensure_single_link_regular_file,
)
from crewplane.artifacts.atomic import atomic_write_json, json_bytes
from crewplane.core.memory.models import Digest, FrozenModel
from crewplane.core.memory.packets import EvidencePacket

from .locking import memory_lock

PACKET_LIMIT_BYTES = 64 * 1024
RECEIPT_LIMIT_BYTES = 16 * 1024 * 1024


class ExtractionReceipt(FrozenModel):
    packet_digest: Digest
    provider_status: Literal["not_started", "succeeded", "failed", "timeout"]
    validation_status: Literal["not_attempted", "succeeded", "failed"] = "not_attempted"
    import_status: Literal["not_attempted", "succeeded", "failed"] = "not_attempted"
    extraction_run_key: str | None = None
    extraction_run_id: str | None = None
    manifest_committed: bool = Field(default=False, strict=True)
    cleanup_confirmed: bool | None = Field(default=None, strict=True)
    lock_released: bool | None = Field(default=None, strict=True)
    result_displayed: bool = Field(default=False, strict=True)
    message: str | None = None

    @property
    def completion_eligible(self) -> bool:
        return all(
            (
                self.manifest_committed,
                self.cleanup_confirmed,
                self.lock_released,
                self.result_displayed,
            )
        )


@dataclass
class ExtractionAttempt:
    stages_dir: Path
    active: bool = False


@asynccontextmanager
async def extraction_attempt(stages_dir: Path) -> AsyncIterator[ExtractionAttempt]:
    directory = ensure_contained_directory(stages_dir, "manifests/memory")
    path = ensure_single_link_regular_file(directory / "extraction.lock")
    async with memory_lock(path, timeout_seconds=0):
        attempt = ExtractionAttempt(stages_dir, active=True)
        try:
            yield attempt
        finally:
            attempt.active = False


def publish_receipt(stages_dir: Path, filename: str, payload: object) -> Path:
    if filename not in {"evidence.json", "extraction.json", "candidates.json"}:
        raise ValueError("Unsupported memory receipt name.")
    ensure_contained_directory(stages_dir, "manifests/memory")
    return atomic_write_json(
        stages_dir / "manifests" / "memory" / filename, payload, ensure_parent=False
    )


def read_packet(stages_dir: Path) -> EvidencePacket:
    return read_model_receipt(
        stages_dir, "manifests/memory/evidence.json", PACKET_LIMIT_BYTES, EvidencePacket
    )


def save_packet(stages_dir: Path, packet: EvidencePacket) -> None:
    payload = packet.model_dump(mode="json")
    if len(json_bytes(payload)) > PACKET_LIMIT_BYTES:
        raise ValueError(
            "Memory evidence packet exceeds 64 KiB; mandatory context cannot be silently truncated."
        )
    publish_receipt(stages_dir, "evidence.json", payload)


def read_extraction(stages_dir: Path) -> ExtractionReceipt | None:
    try:
        return read_model_receipt(
            stages_dir,
            "manifests/memory/extraction.json",
            RECEIPT_LIMIT_BYTES,
            ExtractionReceipt,
        )
    except FileNotFoundError:
        return None


def save_extraction(attempt: ExtractionAttempt, receipt: ExtractionReceipt) -> None:
    if not attempt.active:
        raise ValueError(
            "Extraction receipt publication requires its active owning attempt."
        )
    publish_receipt(
        attempt.stages_dir, "extraction.json", receipt.model_dump(mode="json")
    )


def read_model_receipt[T: FrozenModel](
    root: Path, relative: str, max_bytes: int, model: type[T]
) -> T:
    payload = json.loads(read_contained_bytes(root, relative, max_bytes))
    receipt = model.model_validate(payload)
    if payload != receipt.model_dump(mode="json"):
        raise ValueError(
            f"Memory receipt {relative} has an incomplete or malformed current shape."
        )
    return receipt
