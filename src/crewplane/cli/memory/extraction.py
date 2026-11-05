from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.artifacts.memory.evidence import (
    SOURCE_BYTE_LIMIT,
    NoEligibleEvidence,
    capture_evidence,
)
from crewplane.artifacts.memory.imports import import_extraction
from crewplane.artifacts.memory.outcomes import STRUCTURED_LIMIT_BYTES
from crewplane.artifacts.memory.receipts import (
    ExtractionAttempt,
    ExtractionReceipt,
    extraction_attempt,
    read_extraction,
    save_extraction,
)
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.artifacts.naming import node_state_relative_path, validate_run_key_name
from crewplane.artifacts.verification import read_verified_node_artifact
from crewplane.cli.run.completion import CompletedRun, RunArtifactLocations
from crewplane.cli.run.execution import execute_workflow_run
from crewplane.cli.run.observability import WorkflowCancelledByUser
from crewplane.cli.templates import TEMPLATE_DIR, render_template_content
from crewplane.core.config import Config, require_memory_extractor
from crewplane.core.execution_state import NodeState, RunManifest
from crewplane.core.memory.candidates import parse_candidates
from crewplane.core.memory.lifecycle import CandidateIdentity
from crewplane.core.memory.packets import EvidencePacket
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.workflow.markdown import parse_workflow_markdown_text
from crewplane.core.workflow.models import ProviderSpec, WorkflowPlan
from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup

from .rendering import print_literal


class ExtractionCleanupError(RuntimeError):
    """The extractor did not confirm required cleanup or execution-lock release."""


@dataclass(frozen=True)
class ExtractionResult:
    candidates: tuple[CandidateIdentity, ...] = ()
    message: str = ""


def extraction_source(config: Config) -> PreflightWorkflowSource:
    extractor = require_memory_extractor(config)
    path = TEMPLATE_DIR / "internal" / "memory-extraction.task.md"
    text = render_template_content(path.read_text(encoding="utf-8"))
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
    require_extraction_completion(receipt)
    if receipt.extraction_run_key is None:
        raise ValueError("Extraction receipt has no retained run reference.")
    key = validate_run_key_name(receipt.extraction_run_key)
    stages = state_dir / "execution-stages" / key
    results = state_dir / "execution-results" / key
    plan = PreflightExecutionPlan.model_validate_json(
        read_contained_bytes(stages, "preflight/execution-plan.json", SOURCE_BYTE_LIMIT)
    )
    manifest = RunManifest.model_validate_json(
        read_contained_bytes(stages, "manifests/run.json", SOURCE_BYTE_LIMIT)
    )
    if (
        plan.run_id != receipt.extraction_run_id
        or manifest.run_id != plan.run_id
        or manifest.status != "succeeded"
        or manifest.workflow_signature != plan.workflow_signature
        or manifest.run_key_name != key
        or plan.run_key_name != key
        or len(plan.nodes) != 1
        or plan.nodes[0].id != "extract"
        or plan.nodes[0].memory
    ):
        raise ValueError(
            "Retained extraction run does not match the successful one-node memory-disabled task."
        )
    evidence_values = [
        fragment.value_stored
        for render in plan.render_plans
        for stream in render.streams
        for fragment in stream.fragments
        if fragment.kind == "static_var" and fragment.key == "memory_evidence"
    ]
    if not evidence_values or any(
        value != packet.model_dump_json(indent=2) for value in evidence_values
    ):
        raise ValueError(
            "Retained extraction input disagrees with the saved host evidence packet."
        )
    node = plan.nodes[0]
    state = NodeState.model_validate_json(
        read_contained_bytes(
            stages, node_state_relative_path(node.id).as_posix(), STRUCTURED_LIMIT_BYTES
        )
    )
    if (
        state.run_id != plan.run_id
        or state.run_key_name != plan.run_key_name
        or state.workflow_signature != plan.workflow_signature
    ):
        raise ValueError(
            "Retained extraction node state identity does not match its child run."
        )
    return read_verified_node_artifact(
        stages,
        results,
        NodeArtifactRequest(node.id, node.artifact_contract),
        "output",
        SOURCE_BYTE_LIMIT,
        STRUCTURED_LIMIT_BYTES,
    ).payload.decode("utf-8")


async def extract_memory(
    config: Config, completion: CompletedRun, console: Console, regenerate: bool = False
) -> ExtractionResult:
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
            packet = capture_evidence(
                completion.plan, completion.stages_dir, completion.results_dir
            )
        except NoEligibleEvidence as exc:
            return ExtractionResult(message=str(exc))
        print_literal(
            console,
            f"Project memory: extracting lessons from retained run {packet.parent_run_id}; candidates require human review.",
        )
        previous = read_extraction(completion.stages_dir)
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
            markdown = retained_extraction_result(
                completion.state_dir, previous, packet
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
    if parent.stages_dir is None:
        raise ValueError("Parent artifacts are required for extraction.")
    source = extraction_source(config)
    # Before a child acquires resources, a completed preflight failure is safe to retry.
    # Execution's finally block replaces these facts with its own cleanup/release result.
    locations = RunArtifactLocations(cleanup_confirmed=True, lock_released=True)
    receipt = ExtractionReceipt(
        packet_digest=packet.packet_digest, provider_status="not_started"
    )
    save_extraction(attempt, receipt)
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
        cleanup = unconfirmed_process_cleanup(exc)
        status = "timeout" if isinstance(exc, TimeoutError) else "failed"
        receipt = extraction_attempt_receipt(
            packet, locations, status, str(exc) or type(exc).__name__
        )
        if cleanup is not None:
            receipt = receipt.model_copy(update={"cleanup_confirmed": False})
        try:
            save_extraction(attempt, receipt)
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
        save_extraction(attempt, receipt)
    finally:
        if not child.cleanup_confirmed or not child.lock_released:
            raise ExtractionCleanupError(
                "Memory extraction cleanup or execution-lock release is unconfirmed; parent manifest is unchanged."
            )
    if not child.can_learn or receipt.provider_status != "succeeded":
        return ExtractionResult(
            message="Memory extraction failed; retained artifacts are available for explicit retry."
        )
    markdown = retained_extraction_result(parent.state_dir, receipt, packet)
    return await validate_and_import(parent, packet, receipt, markdown, attempt)


def extraction_attempt_receipt(
    packet: EvidencePacket,
    locations: RunArtifactLocations,
    status: str,
    message: str | None,
) -> ExtractionReceipt:
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
    require_extraction_completion(receipt)
    if parent.stages_dir is None or receipt.extraction_run_id is None:
        raise ValueError("Extraction requires retained parent and child identities.")
    try:
        document = parse_candidates(markdown)
        available = {item.evidence_id for item in packet.evidence}
        if any(
            not proposal.evidence_ids or set(proposal.evidence_ids) - available
            for proposal in document.lessons
        ):
            raise ValueError(
                "Every extracted lesson must cite known host evidence IDs."
            )
    except ValueError as exc:
        save_extraction(
            attempt,
            receipt.model_copy(
                update={"validation_status": "failed", "message": str(exc)}
            ),
        )
        return ExtractionResult(
            message=f"Memory candidate validation failed: {exc}. Retry revalidates this retained result; use --regenerate explicitly for a new model call."
        )
    validated = receipt.model_copy(update={"validation_status": "succeeded"})
    save_extraction(attempt, validated)
    try:
        candidates = await import_extraction(
            parent.project_root,
            parent.stages_dir,
            packet,
            markdown,
            receipt.extraction_run_id,
        )
    except (OSError, ValueError) as exc:
        save_extraction(
            attempt,
            validated.model_copy(
                update={"import_status": "failed", "message": str(exc)}
            ),
        )
        return ExtractionResult(message=f"Memory candidate import failed: {exc}")
    save_extraction(
        attempt,
        validated.model_copy(update={"import_status": "succeeded", "message": None}),
    )
    return ExtractionResult(
        candidates,
        f"Project memory: {len(candidates)} candidate identities retained. New proposals are pending; existing dispositions are unchanged."
        if candidates
        else "Project memory: no useful lesson proposed.",
    )
