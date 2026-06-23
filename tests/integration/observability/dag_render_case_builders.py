from __future__ import annotations

from pathlib import Path
from typing import Any

from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)

CaseData = dict[str, Any]


def provider(name: str, role: str = "executor") -> ProviderSpec:
    return ProviderSpec(provider=name, role=role)


def build_single_node_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="single.node",
        nodes=[
            WorkflowNode(
                id="backend.deploy",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role="shared", content="Deploy backend.")
                ],
                providers=[provider("codex")],
            )
        ],
    )


def build_parallel_roots_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="parallel.roots",
        nodes=[
            WorkflowNode(
                id="backend.auth",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Auth")],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="backend.billing",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Billing")],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="backend.payments",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Payments")],
                providers=[provider("gemini")],
            ),
            WorkflowNode(
                id="frontend.ui",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="UI")],
                providers=[provider("codex")],
            ),
        ],
    )


def build_linear_chain_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="linear.chain",
        nodes=[
            WorkflowNode(
                id="design.discovery",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Discover")],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="design.iteration",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Iterate")],
                needs=["design.discovery"],
                providers=[provider("codex", role="executor")],
            ),
            WorkflowNode(
                id="design.decision",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Decide")],
                needs=["design.iteration"],
                providers=[provider("claude")],
            ),
        ],
    )


def build_fanout_chain_fanin_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="fanout.chain.fanin",
        nodes=[
            WorkflowNode(
                id="implement.plan",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Plan")],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="implement.build",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Build")],
                needs=["implement.plan"],
                providers=[provider("codex"), provider("gemini")],
            ),
            WorkflowNode(
                id="implement.review",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review")],
                needs=["implement.build"],
                providers=[provider("codex", role="executor")],
            ),
            WorkflowNode(
                id="implement.fixes",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Fix")],
                needs=["implement.review"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="implement.handoff",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Handoff")],
                needs=["implement.plan", "implement.fixes"],
                providers=[provider("claude", role="executor")],
            ),
        ],
    )


def build_diamond_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="diamond",
        nodes=[
            WorkflowNode(
                id="data.extract",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Extract")],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="data.transform",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Transform")],
                needs=["data.extract"],
                providers=[provider("gemini")],
            ),
            WorkflowNode(
                id="data.validate",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Validate")],
                needs=["data.extract"],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="data.load",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Load")],
                needs=["data.transform", "data.validate"],
                providers=[provider("claude", role="executor")],
            ),
        ],
    )


def build_independent_roots_fanin_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="independent.roots.fanin",
        nodes=[
            WorkflowNode(
                id="service.auth",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Auth")],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="service.billing",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Billing")],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="service.cache",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Cache")],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="gateway.compose",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Compose")],
                needs=["service.auth", "service.billing", "service.cache"],
                providers=[provider("gemini")],
            ),
        ],
    )


def build_parallel_chain_sidecar_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="parallel.chain.sidecar",
        nodes=[
            WorkflowNode(
                id="plan",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Plan")],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="impl.api",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="API")],
                needs=["plan"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="impl.api-test",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="API test")],
                needs=["impl.api"],
                providers=[provider("codex", role="executor")],
            ),
            WorkflowNode(
                id="impl.frontend",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Frontend")],
                needs=["plan"],
                providers=[provider("gemini")],
            ),
            WorkflowNode(
                id="impl.docs",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Docs")],
                needs=["plan"],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="release",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Release")],
                needs=["impl.api-test", "impl.frontend", "impl.docs"],
                providers=[provider("codex")],
            ),
        ],
    )


def build_asymmetric_fanout_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="asymmetric.fanout",
        nodes=[
            WorkflowNode(
                id="design",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Design")],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="build.backend",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Build backend")],
                needs=["design"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="test.backend",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Test backend")],
                needs=["build.backend"],
                providers=[provider("codex", role="executor")],
            ),
            WorkflowNode(
                id="deploy.backend",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role="shared", content="Deploy backend")
                ],
                needs=["test.backend"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="build.frontend",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role="shared", content="Build frontend")
                ],
                needs=["design"],
                providers=[provider("gemini"), provider("claude")],
            ),
            WorkflowNode(
                id="integration",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Integrate")],
                needs=["deploy.backend", "build.frontend"],
                providers=[provider("claude")],
            ),
        ],
    )


def build_independent_subgraphs_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="independent.subgraphs",
        nodes=[
            WorkflowNode(
                id="backend.api",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="API")],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="backend.deploy",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role="shared", content="Deploy backend")
                ],
                needs=["backend.api"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="frontend.build",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role="shared", content="Build frontend")
                ],
                providers=[provider("gemini")],
            ),
            WorkflowNode(
                id="frontend.deploy",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role="shared", content="Deploy frontend")
                ],
                needs=["frontend.build"],
                providers=[provider("gemini")],
            ),
        ],
    )


def build_nested_fanout_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="nested.fanout",
        nodes=[
            WorkflowNode(
                id="plan",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Plan")],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="impl.frontend",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Frontend")],
                needs=["plan"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="impl.tests",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Tests")],
                needs=["impl.frontend"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="impl.backend",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Backend")],
                needs=["plan"],
                providers=[provider("gemini")],
            ),
            WorkflowNode(
                id="review",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Review")],
                needs=["impl.tests", "impl.backend"],
                providers=[provider("claude")],
            ),
        ],
    )


def build_overflow_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    children = [
        WorkflowNode(
            id=f"child.{index}",
            mode="parallel",
            prompt_segments=[PromptSegment(role="shared", content=f"Child {index}")],
            needs=["root"],
            providers=[provider("codex")],
        )
        for index in range(8)
    ]
    return WorkflowPlan(
        name="overflow",
        nodes=[
            WorkflowNode(
                id="root",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Root")],
                providers=[provider("codex")],
            ),
            *children,
            WorkflowNode(
                id="merge",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Merge")],
                needs=["root", *(child.id for child in children)],
                providers=[provider("claude", role="executor")],
            ),
        ],
    )


def build_failure_propagation_workflow(case_root: Path) -> WorkflowPlan:  # noqa: ARG001 - Required by visualization case builder signature.
    return WorkflowPlan(
        name="failure.propagation",
        nodes=[
            WorkflowNode(
                id="compile.api",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Compile API")],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="deploy.api",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Deploy API")],
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
                prompt_segments=[PromptSegment(role="shared", content="Provision")],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="app.deploy",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Deploy app")],
                needs=["infra.provision"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="smoke.test",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Smoke test")],
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
                prompt_segments=[PromptSegment(role="shared", content="Plan build")],
                providers=[provider("claude")],
            ),
            WorkflowNode(
                id="build.api",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Build API")],
                needs=["build.plan"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="build.worker",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Build worker")],
                needs=["build.plan"],
                providers=[provider("gemini")],
            ),
            WorkflowNode(
                id="build.integrate",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Integrate")],
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
                prompt_segments=[PromptSegment(role="shared", content="Setup infra")],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="deploy.api",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Deploy API")],
                needs=["setup.infra"],
                providers=[provider("codex")],
            ),
            WorkflowNode(
                id="verify.e2e",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="Verify E2E")],
                needs=["deploy.api"],
                providers=[provider("claude")],
            ),
        ],
    )
