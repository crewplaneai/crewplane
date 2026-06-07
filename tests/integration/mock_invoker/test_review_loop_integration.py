from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import yaml
from rich.console import Console

from orchestrator_cli.bootstrap.container import build_runtime_components
from orchestrator_cli.core.config import Config, load_config
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.runtime.execution.workflow import execute_workflow
from tests.helpers.observability import topology_from_workflow
from tests.integration.compiled_plan_helpers import compile_plan_for_components

CONFIG_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1] / "observability" / "fixtures" / "config.yml"
)
FIXTURE_OUTPUT_DIR = Path(__file__).with_name("fixtures") / "review-loop"


def provider(name: str, role: str) -> ProviderSpec:
    return ProviderSpec(provider=name, role=role)


def _load_mock_review_config(project_root: Path, fixture_output_dir: Path) -> Config:
    with CONFIG_TEMPLATE_PATH.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise AssertionError(
            f"Config fixture must deserialize to a mapping: {CONFIG_TEMPLATE_PATH}"
        )

    invoker_spec = data["settings"]["integrations"]["invoker"]  # type: ignore[index]
    if not isinstance(invoker_spec, dict):
        raise AssertionError("Mock invoker fixture must define an invoker mapping.")
    invoker_spec["implementation"] = "mock"

    options = invoker_spec["options"]
    if not isinstance(options, dict):
        raise AssertionError("Mock invoker fixture options must be a mapping.")
    options.update(
        {
            "delay_seconds": 0,
            "output_mode": "file",
            "output_dir": str(fixture_output_dir),
            "strict_file_mode": True,
        }
    )

    config_path = project_root / ".orchestrator" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_config(config_path)


def test_mock_invoker_review_loop_integration_groups_multi_audit_round_artifacts(
    tmp_path: Path,
) -> None:
    config = _load_mock_review_config(tmp_path, FIXTURE_OUTPUT_DIR)
    workflow = WorkflowPlan(
        name="mock.review.loop",
        nodes=[
            WorkflowNode(
                id="review.iterate",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role="shared", content="Review this implementation.")
                ],
                depth=1,
                audit_rounds=2,
                providers=[
                    provider("codex", "executor"),
                    provider("claude", "reviewer"),
                ],
            )
        ],
    )
    components = build_runtime_components(
        config=config,
        workflow_topology=topology_from_workflow(workflow),
        orchestrator_dir=tmp_path / ".orchestrator",
        project_root=tmp_path,
        console=Console(
            file=io.StringIO(),
            force_terminal=False,
            color_system=None,
            width=120,
        ),
        no_live=True,
    )

    plan, secret_context = compile_plan_for_components(
        config=config,
        workflow=workflow,
        components=components,
        project_root=tmp_path,
    )
    asyncio.run(
        execute_workflow(
            plan=plan,
            output=components.artifact_store,
            invoker=components.base_invoker,
            secret_context=secret_context,
            run_id=components.artifact_store.run_id,
            suppress_progress_output=True,
        )
    )

    stage_dir = components.artifact_store.get_stage_dir("review.iterate")
    assert stage_dir is not None
    audit_round_1 = stage_dir / "review-audit-round-1"
    audit_round_2 = stage_dir / "review-audit-round-2"
    assert audit_round_1.exists()
    assert audit_round_2.exists()

    review_state_dir_1 = audit_round_1 / "review-state"
    review_state_dir_2 = audit_round_2 / "review-state"
    assert review_state_dir_1.exists()
    assert review_state_dir_2.exists()

    state_round_1 = review_state_dir_1 / "claude-reviewer-0-round-1.state.json"
    state_round_2 = review_state_dir_1 / "claude-reviewer-0-round-2.state.json"
    inbox_round_1 = review_state_dir_1 / "review-inbox-round-1.md"
    audit_2_state_round_1 = review_state_dir_2 / "claude-reviewer-0-round-1.state.json"

    assert state_round_1.exists()
    assert state_round_2.exists()
    assert inbox_round_1.exists()
    assert audit_2_state_round_1.exists()

    state_payload = json.loads(state_round_1.read_text(encoding="utf-8"))
    assert state_payload["verdict"] == "CHANGES_REQUESTED"
    assert state_payload["major_issues"] == "- Add missing regression tests"
    assert state_payload["nitpicks"] == "- Optional polish"
    assert len(state_payload["unresolved_fingerprints"]) == 1
    assert state_payload["audit_round_num"] == 1

    raw_round_1 = (audit_round_1 / "claude_reviewer_0_round1.raw.txt").read_text(
        encoding="utf-8"
    )
    raw_round_2 = (audit_round_1 / "claude_reviewer_0_round2.raw.txt").read_text(
        encoding="utf-8"
    )
    raw_audit_2_round_1 = (
        audit_round_2 / "claude_reviewer_0_round1.raw.txt"
    ).read_text(encoding="utf-8")
    assert raw_round_1.startswith("Role round fallback used.")
    assert raw_round_2.startswith("Audit round 1 default reviewer used.")
    assert raw_audit_2_round_1.startswith("Fresh audit reviewer used.")

    inbox_text = inbox_round_1.read_text(encoding="utf-8")
    assert "Add missing regression tests" in inbox_text
    assert (
        f"current-output: {audit_round_1 / 'codex_executor_0_round1.md'}" in inbox_text
    )

    audit_2_executor = audit_round_2 / "codex_executor_0_round1.md"
    assert (
        audit_2_executor.read_text(encoding="utf-8")
        == "Executor round 2 output with fixes.\n"
    )

    log_dir = stage_dir / "logs" / "claude"
    assert (log_dir / "claude-reviewer-0-audit1-round1.log").exists()
    assert (log_dir / "claude-reviewer-0-audit1-round2.log").exists()
    assert (log_dir / "claude-reviewer-0-audit2-round1.log").exists()

    result_text = components.artifact_store.get_stage_output_path(
        "review.iterate"
    ).read_text(encoding="utf-8")
    assert "Review Inbox" not in result_text
    assert "## codex_executor_0" in result_text
    assert "## claude_reviewer_0" in result_text
    assert "Executor round 2 output with fixes." in result_text
    assert "VERDICT: NO_FINDINGS" in result_text


def test_mock_invoker_review_loop_integration_keeps_last_valid_candidate_after_invalid_round_and_drift(
    tmp_path: Path,
) -> None:
    config = _load_mock_review_config(tmp_path, FIXTURE_OUTPUT_DIR)
    workflow = WorkflowPlan(
        name="mock.review.loop.drift",
        nodes=[
            WorkflowNode(
                id="review.drift",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role="shared", content="Review this implementation.")
                ],
                depth=1,
                audit_rounds=2,
                providers=[
                    provider("codex", "executor"),
                    provider("claude", "reviewer"),
                ],
            )
        ],
    )
    components = build_runtime_components(
        config=config,
        workflow_topology=topology_from_workflow(workflow),
        orchestrator_dir=tmp_path / ".orchestrator",
        project_root=tmp_path,
        console=Console(
            file=io.StringIO(),
            force_terminal=False,
            color_system=None,
            width=120,
        ),
        no_live=True,
    )

    plan, secret_context = compile_plan_for_components(
        config=config,
        workflow=workflow,
        components=components,
        project_root=tmp_path,
    )
    asyncio.run(
        execute_workflow(
            plan=plan,
            output=components.artifact_store,
            invoker=components.base_invoker,
            secret_context=secret_context,
            run_id=components.artifact_store.run_id,
            suppress_progress_output=True,
        )
    )

    stage_dir = components.artifact_store.get_stage_dir("review.drift")
    assert stage_dir is not None
    audit_round_1 = stage_dir / "review-audit-round-1"
    assert not (audit_round_1 / "claude_reviewer_0_round2.md").exists()

    status_payload = json.loads(
        (stage_dir / "review-state" / "review-loop-status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status_payload["invalid_candidate_round_count"] == 1
    assert status_payload["artifact_drift_warning_count"] == 1
    assert status_payload["continued_after_consensus_exhaustion"] is False
    assert status_payload["canonical_executor_outputs"] == [
        {
            "path": "review-audit-round-2/codex_executor_0_round1.md",
            "provider": "codex",
            "role": "executor",
            "task_id": "codex_executor_0",
        }
    ]

    result_text = components.artifact_store.get_stage_output_path(
        "review.drift"
    ).read_text(encoding="utf-8")
    assert "Executor round 1 baseline candidate." in result_text
    assert "Updated prior artifact in place." not in result_text
