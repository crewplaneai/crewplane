from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from crewplane.architecture.safe_files import ensure_contained_directory
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.core.memory.models import Digest, FrozenModel, Nonempty
from crewplane.version import SCHEMA_VERSION

from .receipts import read_model_receipt


class RunCompletionReceipt(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    run_id: Nonempty
    run_key_name: Nonempty
    workflow_signature: Digest
    outcome: Literal["succeeded", "failed"]
    manifest_committed: bool = Field(strict=True)
    cleanup_confirmed: bool = Field(strict=True)
    lock_released: bool = Field(strict=True)
    result_displayed: bool = Field(strict=True)

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported completion schema; expected {SCHEMA_VERSION!r}."
            )
        return value

    @property
    def eligible(self) -> bool:
        return all(
            (
                self.manifest_committed,
                self.cleanup_confirmed,
                self.lock_released,
                self.result_displayed,
            )
        )


def save_completion(stages_dir: Path, receipt: RunCompletionReceipt) -> None:
    ensure_contained_directory(stages_dir, "manifests")
    atomic_write_json(
        stages_dir / "manifests/completion.json",
        receipt.model_dump(mode="json"),
        ensure_parent=False,
    )


def read_completion(stages_dir: Path) -> RunCompletionReceipt:
    try:
        return read_model_receipt(
            stages_dir, "manifests/completion.json", 64 * 1024, RunCompletionReceipt
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "Retained run completion proof is missing; historical cleanup and own-handle lock release cannot be inferred. Run a new memory-enabled workflow after resolving cleanup."
        ) from exc
