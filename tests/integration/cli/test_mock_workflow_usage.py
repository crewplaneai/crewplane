import asyncio
import hashlib
import io
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from rich.console import Console

import crewplane.cli.templates as templates
import crewplane.cli.workflow_runner as workflow_runner
from crewplane.adapters.invokers.mock_invoker.invoker import MockAgentInvoker
from crewplane.architecture.contracts import InvocationContext
from crewplane.core.config import AgentConfig, Config, load_config
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.core.workflow.validation import validate_workflow_plan
from crewplane.runtime.agent import usage
from tests.helpers.fixture_files import fixture_path
from tests.helpers.working_directory import temporary_project_cwd

type InvocationKey = tuple[str, str, int | None, int | None]


@dataclass(frozen=True)
class ExpectedInvocation:
    node_id: str
    task_id: str
    provider: str
    role: str
    audit_round_num: int | None = None
    round_num: int | None = 1

    @property
    def key(self) -> InvocationKey:
        return self.node_id, self.task_id, self.audit_round_num, self.round_num


EXPECTED_INVOCATIONS = (
    ExpectedInvocation("review.context", "claude_executor_0", "claude", "executor"),
    ExpectedInvocation("review.context", "codex_executor_1", "codex", "executor"),
    ExpectedInvocation("review.context", "gemini_executor_2", "gemini", "executor"),
    ExpectedInvocation("review.iterate", "codex_executor_0", "codex", "executor", 1),
    ExpectedInvocation("review.iterate", "claude_reviewer_0", "claude", "reviewer", 1),
    ExpectedInvocation("review.iterate", "gemini_reviewer_1", "gemini", "reviewer", 1),
    ExpectedInvocation("review.summary", "claude_executor_0", "claude", "executor"),
)


@dataclass(frozen=True)
class CapturedInvocation:
    context: InvocationContext
    prompt: str
    response: str


@dataclass(frozen=True)
class UsageRun:
    stage_root: Path
    results_root: Path
    invocations: dict[InvocationKey, CapturedInvocation]
    console_output: str


@contextmanager
def capture_mock_invocations() -> Iterator[dict[InvocationKey, CapturedInvocation]]:
    captured: dict[InvocationKey, CapturedInvocation] = {}
    original_invoke = MockAgentInvoker.invoke

    async def record_invocation(
        invoker: MockAgentInvoker,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        assert isinstance(invocation_context, InvocationContext)
        await original_invoke(
            invoker,
            config,
            model,
            prompt,
            output_file,
            cwd,
            log_file,
            invocation_context,
        )
        key = (
            invocation_context.node_id,
            invocation_context.task_id,
            invocation_context.audit_round_num,
            invocation_context.round_num,
        )
        assert key not in captured, f"duplicate invocation: {key}"
        captured[key] = CapturedInvocation(
            invocation_context, prompt, output_file.read_text(encoding="utf-8")
        )

    with patch.object(MockAgentInvoker, "invoke", new=record_invocation):
        yield captured


def prepare_usage_project(root: Path) -> tuple[Config, Path]:
    state_dir = root / ".crewplane"
    workflows_dir = state_dir / "workflows"
    workflows_dir.mkdir(parents=True)
    config_path = state_dir / "config.yml"
    workflow_path = workflows_dir / "usage.task.md"
    for source, destination in (
        (templates.CONFIG_TEMPLATE, config_path),
        (fixture_path("usage/workflow.task.md"), workflow_path),
    ):
        destination.write_text(
            templates.render_template_content(source.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    config = load_config(config_path)
    assert config.settings is not None
    config.settings.integrations.invoker.options = {
        "delay_seconds": 0,
        "observation_delay_seconds": 0,
        "output_mode": "lorem",
        "seed": 42,
    }
    for provider in ("claude", "codex", "gemini"):
        config.agents[provider] = config.agents["mock"].model_copy(deep=True)
    return config, workflow_path


def run_usage_scenario(tmp_path: Path) -> UsageRun:
    with temporary_project_cwd(tmp_path) as root:
        config, workflow_path = prepare_usage_project(root)
        source = load_workflow_source_for_preflight(workflow_path, project_root=root)
        validate_workflow_plan(source.workflow)
        stream = io.StringIO()
        with capture_mock_invocations() as captured:
            asyncio.run(
                workflow_runner.execute_workflow_run(
                    config=config,
                    source=source,
                    force=False,
                    no_live=True,
                    console=Console(file=stream, force_terminal=False),
                )
            )
        (stage_root,) = (root / ".crewplane/execution-stages").iterdir()
        (results_root,) = (root / ".crewplane/execution-results").iterdir()
        return UsageRun(stage_root, results_root, captured, stream.getvalue())


@pytest.fixture
def usage_run(tmp_path: Path) -> UsageRun:
    return run_usage_scenario(tmp_path)


def expected_visible_tokens(invocation: CapturedInvocation) -> int:
    # The documented lower bound rounds each visible text separately.
    return (len(invocation.prompt) + 3) // 4 + (len(invocation.response) + 3) // 4


def assert_usage_record(
    record: dict[str, object],
    captured: CapturedInvocation,
    expected: ExpectedInvocation,
) -> None:
    assert record["visible_estimate_tokens"] == expected_visible_tokens(captured) > 0, (
        "incorrect visible token estimate"
    )
    assert record["attempt_count"] == 1
    assert record["cli_captured"] is True
    assert record["provider_usage_status"] == "none"
    assert record["provider_usage_report_count"] == 0
    assert record["visible_estimate_method"] == "char-count-lower-bound"
    assert record["visible_estimate_is_lower_bound"] is True
    assert record["output_extraction_status"] == "success"
    assert record["provider"] == captured.context.provider == expected.provider
    assert record["role"] == captured.context.role == expected.role


def assert_invocation_usage(run: UsageRun) -> None:
    expected_keys = {invocation.key for invocation in EXPECTED_INVOCATIONS}
    assert set(run.invocations) == expected_keys
    event_log = run.stage_root / "logs/events.ndjson"
    records = [
        json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()
    ]
    finished = [
        record for record in records if record["event_type"] == "invocation_finished"
    ]
    assert len(finished) == len(expected_keys) == 7
    usage_by_key = {
        (
            record["node_id"],
            record["task_id"],
            record.get("audit_round_num"),
            record.get("round_num"),
        ): record
        for record in finished
    }
    assert set(usage_by_key) == expected_keys
    for expected in EXPECTED_INVOCATIONS:
        assert_usage_record(
            usage_by_key[expected.key], run.invocations[expected.key], expected
        )


def test_mock_run_records_estimated_usage(usage_run: UsageRun) -> None:
    assert_invocation_usage(usage_run)


def test_mock_run_emits_findings(usage_run: UsageRun) -> None:
    findings = usage_run.results_root / "review.context-findings.md"
    assert findings.is_file()
    assert "Synthetic finding for review.context" in findings.read_text(
        encoding="utf-8"
    )


def test_mock_run_records_verified_artifacts(usage_run: UsageRun) -> None:
    manifest = json.loads(
        (usage_run.stage_root / "manifests/run.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "succeeded"
    states = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (usage_run.stage_root / "manifests/nodes").glob("*.json")
    ]
    assert {state["node_id"] for state in states} == {
        invocation.node_id for invocation in EXPECTED_INVOCATIONS
    }
    for state in states:
        assert state["run_id"] == manifest["run_id"]
        assert state["artifacts"]
        for artifact in state["artifacts"]:
            assert artifact["relative_path"].startswith(state["node_id"] + "-")
            payload = (usage_run.results_root / artifact["relative_path"]).read_bytes()
            assert len(payload) == artifact["size_bytes"]
            assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]


def test_mock_run_renders_usage_totals(usage_run: UsageRun) -> None:
    expected_total = sum(
        expected_visible_tokens(invocation)
        for invocation in usage_run.invocations.values()
    )
    summary = (usage_run.stage_root / "logs/summary.md").read_text(encoding="utf-8")
    assert "CLI invocations captured: 7/7" in summary
    assert f"Visible-text estimate (lower-bound): {expected_total:,} tokens" in summary
    assert "Run Summary" in usage_run.console_output
    assert f"{expected_total:,} tokens" in usage_run.console_output


def test_usage_assertions_reject_zero_estimates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage, "estimate_token_count", Mock(return_value=0))
    run = run_usage_scenario(tmp_path)
    with pytest.raises(AssertionError, match="incorrect visible token estimate"):
        assert_invocation_usage(run)
