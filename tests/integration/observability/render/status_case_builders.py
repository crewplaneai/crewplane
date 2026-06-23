from __future__ import annotations

from pathlib import Path

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.models import (
    PromptSegment,
    WorkflowNode,
    WorkflowPlan,
)
from tests.integration.observability.render.case_types import provider


def build_failure_propagation_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="failure.propagation",
        nodes=[
            WorkflowNode(
                id="compile.api",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Compile API")
                ],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="deploy.api",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Deploy API")
                ],
                needs=["compile.api"],
                providers=[provider("claude")],
            ),
        ],
    )


def build_cascading_failure_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="cascading.failure",
        nodes=[
            WorkflowNode(
                id="infra.provision",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Provision")
                ],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="app.deploy",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Deploy app")
                ],
                needs=["infra.provision"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="smoke.test",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Smoke test")
                ],
                needs=["app.deploy"],
                providers=[provider("claude")],
            ),
        ],
    )


def build_partial_failure_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="partial.failure",
        nodes=[
            WorkflowNode(
                id="build.plan",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Plan build")
                ],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="build.api",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Build API")
                ],
                needs=["build.plan"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="build.worker",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Build worker")
                ],
                needs=["build.plan"],
                providers=[provider("gemini")],
            ),
            WorkflowNode(
                id="build.integrate",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Integrate")
                ],
                needs=["build.api", "build.worker"],
                providers=[provider("claude")],
            ),
        ],
    )


def build_pending_running_succeeded_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="pending.running.succeeded",
        nodes=[
            WorkflowNode(
                id="setup.infra",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Setup infra")
                ],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="deploy.api",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Deploy API")
                ],
                needs=["setup.infra"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="verify.e2e",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Verify E2E")
                ],
                needs=["deploy.api"],
                providers=[provider("claude")],
            ),
        ],
    )
