from __future__ import annotations

from pathlib import Path

from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import (
    load_workflow_source_for_preflight,
)
from crewplane.core.preflight.models import (
    PreflightCompilationPreview,
)
from crewplane.core.preflight.plan_signatures import semantic_node_payloads
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION
from tests.unit.core.preflight.workspace_preflight_signatures_support import (
    compile_signature_workflow,
    compile_source_with_source_snapshot,
)


def test_review_starts_with_changes_workflow_signature(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("review context\n", encoding="utf-8")
    config = review_loop_config()
    omitted = compile_signature_workflow(tmp_path, review_loop_workflow(), config)
    explicit_executor = compile_signature_workflow(
        tmp_path,
        review_loop_workflow("executor"),
        config,
    )
    reviewer_first = compile_signature_workflow(
        tmp_path,
        review_loop_workflow("reviewer"),
        config,
    )

    assert omitted.diagnostics == []
    assert explicit_executor.diagnostics == []
    assert reviewer_first.diagnostics == []
    assert omitted.workflow_signature is not None
    assert omitted.workflow_signature == explicit_executor.workflow_signature
    assert omitted.workflow_signature != reviewer_first.workflow_signature
    assert "review_starts_with" not in semantic_execution_policy(omitted)
    assert "review_starts_with" not in semantic_execution_policy(explicit_executor)
    assert semantic_execution_policy(reviewer_first)["review_starts_with"] == "reviewer"


def test_review_loop_audit_ceiling_does_not_change_execution_identity(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("review context\n", encoding="utf-8")
    first = compile_signature_workflow(
        tmp_path,
        review_loop_workflow(),
        review_loop_config(max_audit_rounds=5),
    )
    second = compile_signature_workflow(
        tmp_path,
        review_loop_workflow(),
        review_loop_config(max_audit_rounds=6),
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.effective_runtime_config_signature == (
        second.effective_runtime_config_signature
    )
    assert first.workflow_signature == second.workflow_signature
    assert first.runtime_config_snapshot is not None
    assert second.runtime_config_snapshot is not None
    assert (
        first.runtime_config_snapshot.redacted_payload()["execution"][
            "max_audit_rounds"
        ]
        == 5
    )
    assert (
        second.runtime_config_snapshot.redacted_payload()["execution"][
            "max_audit_rounds"
        ]
        == 6
    )


def test_explicit_executor_default_in_markdown_keeps_workflow_signature(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("review context\n", encoding="utf-8")
    workflow_path = tmp_path / "review.task.md"
    config = review_loop_config()
    workflow_path.write_text(
        markdown_review_loop_workflow(review_starts_with=None),
        encoding="utf-8",
    )
    omitted = compile_source_with_source_snapshot(
        tmp_path,
        load_workflow_source_for_preflight(workflow_path, tmp_path),
        None,
        config,
    )
    workflow_path.write_text(
        markdown_review_loop_workflow(review_starts_with="executor"),
        encoding="utf-8",
    )
    explicit_executor = compile_source_with_source_snapshot(
        tmp_path,
        load_workflow_source_for_preflight(workflow_path, tmp_path),
        None,
        config,
    )

    assert omitted.diagnostics == []
    assert explicit_executor.diagnostics == []
    assert omitted.workflow_signature is not None
    assert omitted.workflow_signature == explicit_executor.workflow_signature


def review_loop_workflow(review_starts_with: str | None = None) -> WorkflowPlan:
    node_payload: dict[str, object] = {
        "id": "review.loop",
        "mode": "sequential",
        "providers": [
            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR),
            ProviderSpec(provider="beta", role=ProviderRole.REVIEWER),
        ],
        "prompt_segments": [
            PromptSegment(role=PromptSegmentRole.SHARED, content="Review the change."),
            PromptSegment(
                role=PromptSegmentRole.REVIEWER,
                content="Inspect {{file:README.md}}.",
            ),
        ],
    }
    if review_starts_with is not None:
        node_payload["review_starts_with"] = review_starts_with
    return WorkflowPlan(
        name="review loop workflow",
        nodes=[WorkflowNode.model_validate(node_payload)],
    )


def markdown_review_loop_workflow(review_starts_with: str | None) -> str:
    review_starts_with_line = (
        f"    review_starts_with: {review_starts_with}\n"
        if review_starts_with is not None
        else ""
    )
    return (
        "---\n"
        f'schema_version: "{SCHEMA_VERSION}"\n'
        "name: review loop workflow\n"
        "nodes:\n"
        "  - id: review.loop\n"
        "    mode: sequential\n"
        f"{review_starts_with_line}"
        "    providers:\n"
        "      - provider: alpha\n"
        "        role: executor\n"
        "      - provider: beta\n"
        "        role: reviewer\n"
        "---\n"
        "\n"
        "## review.loop\n"
        "\n"
        "Review the change.\n"
        "\n"
        "<!-- crewplane:reviewer -->\n"
        "Inspect {{file:README.md}}.\n"
        "<!-- /crewplane:reviewer -->\n"
    )


def review_loop_config(max_audit_rounds: int = 3) -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={
            "alpha": AgentConfig(cli_cmd=["echo"]),
            "beta": AgentConfig(cli_cmd=["echo"]),
        },
        settings=Settings(
            max_audit_rounds=max_audit_rounds,
            workspace={"enabled": False},
        ),
    )


def semantic_execution_policy(
    preview: PreflightCompilationPreview,
) -> dict[str, object]:
    node_payload = semantic_node_payloads(preview.nodes)[0]
    assert isinstance(node_payload, dict)
    execution_policy = node_payload.get("execution_policy")
    assert isinstance(execution_policy, dict)
    return execution_policy
