from __future__ import annotations

import pytest

from orchestrator_cli.core.preflight.models import ExecutionPolicy, TokenBudgetPolicy
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.runtime.execution import prompt_budgeting
from orchestrator_cli.runtime.execution.fragment_assembler import ResolvedPrompt
from orchestrator_cli.runtime.execution.runtime_context import CompiledRuntimeContext
from orchestrator_cli.runtime.execution.workspace_files import ResolvedWorkspaceFile
from tests.helpers.resume import make_plan, make_workspace_file_locator, sha256_hex


def test_prompt_budget_rejects_resolved_workspace_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    locator = make_workspace_file_locator().model_copy(
        update={
            "locator_id": "workspace-file-b",
            "node_id": "b",
            "target": "executor_prompt",
            "byte_size": 20,
            "canonical_blob_sha256": sha256_hex("x" * 20),
        }
    )
    node = plan.nodes[1].model_copy(
        update={
            "execution_policy": ExecutionPolicy(
                token_budget=TokenBudgetPolicy(fail_threshold_chars=10)
            )
        }
    )
    plan = plan.model_copy(
        update={
            "nodes": [plan.nodes[0], node],
            "workspace_file_locators": [locator],
        }
    )
    resolved_file = ResolvedWorkspaceFile(
        locator=locator,
        text="x" * 20,
        byte_size=20,
        sha256=sha256_hex("x" * 20),
    )

    def inspect_runtime_locators_stub(*args: object) -> tuple[object, ...]:
        del args
        return ()

    def assemble_prompt_details_stub(
        *args: object,
        **kwargs: object,
    ) -> ResolvedPrompt:
        del args, kwargs
        return ResolvedPrompt(resolved_file.text, (resolved_file,))

    monkeypatch.setattr(
        prompt_budgeting,
        "inspect_runtime_locators",
        inspect_runtime_locators_stub,
    )
    monkeypatch.setattr(
        prompt_budgeting,
        "assemble_prompt_details",
        assemble_prompt_details_stub,
    )

    with pytest.raises(
        prompt_budgeting.PromptBudgetExceededError,
        match="workspace file 'docs/input.md' resolves to 20 chars",
    ):
        prompt_budgeting.resolve_prompt_with_output_budget_details(
            CompiledRuntimeContext(plan=plan, secret_context=SecretContext()),
            node,
            object(),
            "executor",
            None,
        )
