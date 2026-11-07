from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from rich.console import Console

from crewplane.artifacts.memory.evidence import (
    NoEligibleEvidence,
    capture_evidence,
)
from crewplane.artifacts.memory.imports import import_extraction
from crewplane.artifacts.memory.receipts import (
    ExtractionAttempt,
    ExtractionReceipt,
    extraction_attempt,
    read_extraction,
    save_extraction,
)
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.cli.run.completion import CompletedRun, RunArtifactLocations
from crewplane.cli.run.execution import execute_workflow_run
from crewplane.cli.run.observability import WorkflowCancelledByUser
from crewplane.core.config import Config, require_memory_extractor
from crewplane.core.memory.candidates import parse_candidates
from crewplane.core.memory.lifecycle import CandidateIdentity
from crewplane.core.memory.packets import EvidencePacket
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.workflow.markdown import parse_workflow_markdown_text
from crewplane.core.workflow.models import ProviderSpec, WorkflowPlan
from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup
from crewplane.version import SCHEMA_VERSION

from .extraction_verification import verified_extraction_output
from .rendering import print_literal


class ExtractionCleanupError(RuntimeError):
    """The extractor did not confirm required cleanup or execution-lock release."""


@dataclass(frozen=True)
class ExtractionResult:
    """Retained candidate identities in document order and a displayable outcome.

    Candidates remain subject to human review. An empty tuple can accompany a
    skip, recoverable failure, or successful extraction with no useful lessons;
    ``message`` distinguishes these outcomes without changing the parent run.
    """

    candidates: tuple[CandidateIdentity, ...] = ()
    message: str = ""


def extraction_source(config: Config) -> PreflightWorkflowSource:
    """Read the packaged one-node task and select the configured extractor.

    Missing extractor configuration, workflow validation, and file-read errors
    propagate. No child run or artifacts are created here.
    """
    extractor = require_memory_extractor(config)
    path = (
        Path(__file__).resolve().parents[2]
        / "resources"
        / "workflows"
        / "memory-extraction.task.md"
    )
    text = path.read_text(encoding="utf-8").replace(
        "__SCHEMA_VERSION__", SCHEMA_VERSION
    )
    payload = parse_workflow_markdown_text(path, text)
    workflow = WorkflowPlan.model_validate(
        {key: value for key, value in payload.items() if key != "imports"}
    )
    node = workflow.nodes[0].model_copy(
        update={"providers": [ProviderSpec(provider=extractor)]}
    )
    workflow = workflow.model_copy(update={"nodes": [node]})
    return PreflightWorkflowSource.from_workflow(
        workflow, workflow_content=text, root_workflow_path=path
    )


def extraction_config(config: Config) -> Config:
    """Copy config with learning and workspaces disabled for the child run."""
    settings = config.settings.model_copy(
        update={
            "memory": config.settings.memory.model_copy(update={"learn": False}),
            "workspace": config.settings.workspace.model_copy(
                update={"enabled": False}
            ),
        }
    )
    return config.model_copy(update={"settings": settings})


def retained_extraction_result(
    state_dir: Path, receipt: ExtractionReceipt, packet: EvidencePacket
) -> str:
    """Read verified canonical output without invoking the provider or writing.

    The caller owns the parent extraction-attempt lock. Require a completion-
    eligible receipt and verify the child identity, host evidence, node state,
    and output artifact in that order. Cleanup failures raise
    ExtractionCleanupError; file and validation errors propagate unchanged.
    """
    require_extraction_completion(receipt)
    return verified_extraction_output(state_dir, receipt, packet)


async def extract_memory(
    config: Config, completion: CompletedRun, console: Console, regenerate: bool = False
) -> ExtractionResult:
    """Capture host evidence and reuse or regenerate extraction under one lock.

    Check the initialized store before acquiring the parent extraction lock;
    retain that lock through evidence capture, child execution, and import.
    Successful retained output is revalidated on retry, including after failed
    validation/import; ``regenerate`` explicitly requests a fresh child instead.
    A mismatched receipt or unconfirmed cleanup forbids either path.

    Ineligible parents and absent eligible evidence return messages. Ordinary
    child, validation, and import failures return messages as documented by the
    delegated operations; configuration, integrity, and receipt I/O errors
    propagate. Cancellation drains outstanding file I/O before lock release.
    """
    if (
        not completion.can_learn
        or completion.plan is None
        or completion.stages_dir is None
        or completion.results_dir is None
    ):
        return ExtractionResult(
            message="Memory learning skipped: completed-run cleanup/release prerequisites were not confirmed."
        )
    require_memory_extractor(config)
    await MemoryStore(completion.project_root).snapshot()
    async with extraction_attempt(completion.stages_dir) as attempt:
        try:
            packet = await _complete_extraction_io(
                partial(
                    capture_evidence,
                    completion.plan,
                    completion.stages_dir,
                    completion.results_dir,
                )
            )
        except NoEligibleEvidence as exc:
            return ExtractionResult(message=str(exc))
        print_literal(
            console,
            f"Project memory: extracting lessons from retained run {packet.parent_run_id}; candidates require human review.",
        )
        previous = await _complete_extraction_io(
            partial(read_extraction, completion.stages_dir)
        )
        if previous is not None and previous.packet_digest != packet.packet_digest:
            raise ValueError(
                "Extraction receipt does not match the saved evidence packet."
            )
        if previous is not None and (
            previous.cleanup_confirmed is not True or previous.lock_released is not True
        ):
            raise ExtractionCleanupError(
                "Retained memory extraction cleanup or own-handle execution-lock release is unconfirmed; retry and regeneration cannot infer recovery. Parent manifest is unchanged."
            )
        if (
            previous is not None
            and previous.provider_status == "succeeded"
            and not regenerate
        ):
            markdown = await _complete_extraction_io(
                partial(
                    retained_extraction_result, completion.state_dir, previous, packet
                )
            )
            return await validate_and_import(
                completion, packet, previous, markdown, attempt
            )
        return await execute_extraction(config, completion, packet, console, attempt)


async def execute_extraction(
    config: Config,
    parent: CompletedRun,
    packet: EvidencePacket,
    console: Console,
    attempt: ExtractionAttempt,
) -> ExtractionResult:
    """Run a fresh extractor and persist its attempt before importing output.

    The caller retains the active ``attempt`` and its parent lock. Persist
    not-started, terminal, validation, and import receipts in order; never alter
    the parent manifest. The timeout covers child execution only.

    Ordinary provider errors/timeouts return messages after receipt publication.
    Setup, verification, and receipt errors propagate. Unconfirmed cleanup or
    own-handle release takes precedence over receipt failure and cancellation;
    otherwise process-cleanup errors and user/task cancellation are re-raised
    even if terminal receipt publication fails.
    """
    if parent.stages_dir is None:
        raise ValueError("Parent artifacts are required for extraction.")
    source = await _complete_extraction_io(partial(extraction_source, config))
    # Before a child acquires resources, a completed preflight failure is safe to retry.
    # Execution's finally block replaces these facts with its own cleanup/release result.
    locations = RunArtifactLocations(cleanup_confirmed=True, lock_released=True)
    receipt = ExtractionReceipt(
        packet_digest=packet.packet_digest, provider_status="not_started"
    )
    try:
        await _save_receipt(attempt, receipt)
    except asyncio.CancelledError as exc:
        return await _finalize_failed_extraction(packet, locations, attempt, exc)
    try:
        async with asyncio.timeout(config.settings.memory.extraction_timeout_seconds):
            child = await execute_workflow_run(
                extraction_config(config),
                source,
                force=True,
                no_live=True,
                console=console,
                project_root=parent.project_root,
                state_dir=parent.state_dir,
                runtime_variables={"memory_evidence": packet.model_dump_json(indent=2)},
                defer_terminal_failure=True,
                artifact_locations=locations,
            )
    except (Exception, asyncio.CancelledError) as exc:
        return await _finalize_failed_extraction(packet, locations, attempt, exc)
    receipt = await _record_child_completion(packet, locations, child, attempt)
    if not child.can_learn or receipt.provider_status != "succeeded":
        return ExtractionResult(
            message="Memory extraction failed; retained artifacts are available for explicit retry."
        )
    markdown = await _complete_extraction_io(
        partial(retained_extraction_result, parent.state_dir, receipt, packet)
    )
    return await validate_and_import(parent, packet, receipt, markdown, attempt)


async def _finalize_failed_extraction(
    packet: EvidencePacket,
    locations: RunArtifactLocations,
    attempt: ExtractionAttempt,
    exc: Exception | asyncio.CancelledError,
) -> ExtractionResult:
    cleanup = unconfirmed_process_cleanup(exc)
    status: Literal["timeout", "failed"] = (
        "timeout" if isinstance(exc, TimeoutError) else "failed"
    )
    receipt = extraction_attempt_receipt(
        packet, locations, status, str(exc) or type(exc).__name__
    )
    if cleanup is not None:
        receipt = receipt.model_copy(update={"cleanup_confirmed": False})
    try:
        await _save_receipt(attempt, receipt)
    finally:
        if locations.lock_released is False or locations.cleanup_confirmed is False:
            raise ExtractionCleanupError(
                "Memory extraction cleanup or execution-lock release is unconfirmed; parent manifest is unchanged."
            ) from exc
        if cleanup is not None or isinstance(
            exc, (asyncio.CancelledError, WorkflowCancelledByUser)
        ):
            raise exc
    return ExtractionResult(message=f"Memory extraction {status}: {exc}")


async def _record_child_completion(
    packet: EvidencePacket,
    locations: RunArtifactLocations,
    child: CompletedRun,
    attempt: ExtractionAttempt,
) -> ExtractionReceipt:
    receipt = extraction_attempt_receipt(
        packet,
        locations,
        "succeeded" if child.outcome == "succeeded" else "failed",
        str(child.terminal_error) if child.terminal_error else None,
    ).model_copy(
        update={
            "manifest_committed": child.manifest_committed,
            "cleanup_confirmed": child.cleanup_confirmed,
            "lock_released": child.lock_released,
            "result_displayed": child.result_displayed,
        }
    )
    try:
        await _save_receipt(attempt, receipt)
    finally:
        if not child.cleanup_confirmed or not child.lock_released:
            raise ExtractionCleanupError(
                "Memory extraction cleanup or execution-lock release is unconfirmed; parent manifest is unchanged."
            )
    return receipt


def extraction_attempt_receipt(
    packet: EvidencePacket,
    locations: RunArtifactLocations,
    status: Literal["not_started", "succeeded", "failed", "timeout"],
    message: str | None,
) -> ExtractionReceipt:
    """Validate child identity and cleanup facts into an unpersisted receipt."""
    return ExtractionReceipt.model_validate(
        {
            "packet_digest": packet.packet_digest,
            "provider_status": status,
            "extraction_run_key": locations.plan.run_key_name
            if locations.plan
            else None,
            "extraction_run_id": locations.plan.run_id if locations.plan else None,
            "cleanup_confirmed": locations.cleanup_confirmed,
            "lock_released": locations.lock_released,
            "message": message,
        }
    )


def require_extraction_completion(receipt: ExtractionReceipt) -> None:
    """Raise ExtractionCleanupError unless all four completion proofs hold."""
    if not receipt.completion_eligible:
        raise ExtractionCleanupError(
            "Retained memory extraction completion, cleanup or own-handle execution-lock release was not confirmed; canonical output cannot be reused or imported. Parent manifest is unchanged."
        )


async def validate_and_import(
    parent: CompletedRun,
    packet: EvidencePacket,
    receipt: ExtractionReceipt,
    markdown: str,
    attempt: ExtractionAttempt,
) -> ExtractionResult:
    """Validate evidence references, then import candidates without acceptance.

    Require the caller's active attempt/lock, completion proof, and retained
    parent/child identities. Persist validation success before import and import
    status afterward. Validation ValueError and import OSError/ValueError become
    messages only after their failure receipt is saved. Receipt errors, missing
    identities, cleanup failures, and cancellation propagate. A failed or
    cancelled import may already have committed candidates; retry reuses them.
    """
    require_extraction_completion(receipt)
    if parent.stages_dir is None or receipt.extraction_run_id is None:
        raise ValueError("Extraction requires retained parent and child identities.")
    try:
        _validate_candidate_evidence(markdown, packet)
    except ValueError as exc:
        await _save_receipt(
            attempt,
            receipt.model_copy(
                update={"validation_status": "failed", "message": str(exc)}
            ),
        )
        return ExtractionResult(
            message=f"Memory candidate validation failed: {exc}. Retry revalidates this retained result; use --regenerate explicitly for a new model call."
        )
    validated = receipt.model_copy(update={"validation_status": "succeeded"})
    await _save_receipt(attempt, validated)
    try:
        candidates = await import_extraction(
            parent.project_root,
            parent.stages_dir,
            packet,
            markdown,
            receipt.extraction_run_id,
        )
    except (OSError, ValueError) as exc:
        await _save_receipt(
            attempt,
            validated.model_copy(
                update={"import_status": "failed", "message": str(exc)}
            ),
        )
        return ExtractionResult(message=f"Memory candidate import failed: {exc}")
    await _save_receipt(
        attempt,
        validated.model_copy(update={"import_status": "succeeded", "message": None}),
    )
    return ExtractionResult(
        candidates,
        f"Project memory: {len(candidates)} candidate identities retained. New proposals are pending; existing dispositions are unchanged."
        if candidates
        else "Project memory: no useful lesson proposed.",
    )


def _validate_candidate_evidence(markdown: str, packet: EvidencePacket) -> None:
    document = parse_candidates(markdown)
    available = {item.evidence_id for item in packet.evidence}
    if any(
        not proposal.evidence_ids or set(proposal.evidence_ids) - available
        for proposal in document.lessons
    ):
        raise ValueError("Every extracted lesson must cite known host evidence IDs.")


async def _save_receipt(attempt: ExtractionAttempt, receipt: ExtractionReceipt) -> None:
    await _complete_extraction_io(partial(save_extraction, attempt, receipt))


async def _complete_extraction_io[T](operation: Callable[[], T]) -> T:
    """Drain file I/O before unlocking, then propagate cancellation with its cause.

    Keep the executor future independent of task cancellation, including runner
    shutdown and repeated cancellation while waiting for the worker.
    NoEligibleEvidence is an expected skip and does not override cancellation.
    """
    worker = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, operation
    )
    cancelled: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.wait((worker,))
        except asyncio.CancelledError as exc:
            cancelled = exc
    try:
        result = worker.result()
    except NoEligibleEvidence:
        if cancelled is not None:
            raise cancelled from None
        raise
    except Exception as exc:
        if cancelled is not None:
            raise cancelled from exc
        raise
    if cancelled is not None:
        raise cancelled
    return result
