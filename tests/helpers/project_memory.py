from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import Config
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.lifecycle import new_revision
from crewplane.core.memory.models import (
    Acceptance,
    MemoryRecord,
    MemorySnapshot,
    ProposalProvenance,
    digest_payload,
)
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from crewplane.core.preflight.models import (
    PreflightCompilationPreview,
    PreflightExecutionPlan,
)
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.version import SCHEMA_VERSION


def accepted_memory(*lessons: LessonProposal) -> MemorySnapshot:
    records = []
    for index, lesson in enumerate(lessons):
        revision = new_revision(
            lesson,
            ProposalProvenance(author="human", source_identity="manual"),
            (),
            digest_payload(lesson.model_dump(mode="json")),
        ).model_copy(
            update={"revision_id": f"r-{index}", "created_at": "2026-10-01T00:00:00Z"}
        )
        accepted = revision.model_copy(
            update={
                "disposition": "accepted",
                "acceptance": Acceptance(
                    accepted_at="2026-10-01T00:00:00Z",
                    candidate_digest=revision.candidate_digest,
                    expected_accepted_revision=None,
                ),
            }
        )
        records.append(
            MemoryRecord(
                record_id=f"m-{index}",
                revisions=(accepted,),
                accepted_revision=accepted.revision_id,
            )
        )
    return MemorySnapshot(records=tuple(records))


def memory_config() -> Config:
    return Config.model_validate(
        {
            "version": SCHEMA_VERSION,
            "agents": {"alpha": {"cli_cmd": ["mock"]}, "beta": {"cli_cmd": ["mock"]}},
            "settings": {
                "memory": {"learn": False},
                "max_concurrent_nodes": 1,
                "integrations": {"invoker": {"implementation": "mock"}},
            },
        }
    )


def compile_memory_workflow(
    root: Path,
    workflow: WorkflowPlan | PreflightWorkflowSource,
    memory: MemorySnapshot | None,
    config: Config | None = None,
    variables: dict[str, str] | None = None,
) -> PreflightCompilationPreview:
    config = config or memory_config()
    runtime = build_runtime_config_snapshot(config, Console(), no_live=True).snapshot
    source = (
        workflow
        if isinstance(workflow, PreflightWorkflowSource)
        else PreflightWorkflowSource.from_workflow(workflow)
    )
    return compile_preflight_preview(
        source,
        config,
        runtime,
        PreflightCompileOptions(
            root,
            root / ".crewplane",
            memory_snapshot=memory,
            runtime_variables=variables or {},
            environment={"TOKEN": "secret-context"},
        ),
    )


def memory_plan(
    preview: PreflightCompilationPreview, root: Path
) -> PreflightExecutionPlan:
    return PreflightExecutionPlan.from_preview(
        preview,
        "run",
        "run",
        root.as_posix(),
        root.as_posix(),
        (root / "manifests").as_posix(),
        datetime(2026, 10, 1, tzinfo=UTC),
    )


def memory_workflow(prompt: str, review: str | None = None) -> WorkflowPlan:
    segments = [{"role": "executor" if review else "shared", "content": prompt}]
    providers = [{"provider": "alpha"}]
    if review:
        segments.append({"role": "reviewer", "content": review})
        providers.append({"provider": "beta", "role": "reviewer"})
    return WorkflowPlan.model_validate(
        {
            "name": "Recall",
            "memory": True,
            "nodes": [
                {
                    "id": "work",
                    "mode": "sequential",
                    "providers": providers,
                    "prompt_segments": segments,
                }
            ],
        }
    )
