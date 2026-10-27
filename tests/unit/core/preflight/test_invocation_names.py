from datetime import datetime

import pytest
from pydantic import ValidationError
from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import AgentConfig
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightExecutionPlan,
    compile_preflight_preview,
)
from crewplane.core.workflow.models import ProviderSpec
from tests.helpers.invocation_names import boundary_alias
from tests.integration.architecture.preflight_plan.helpers import (
    literal_workflow,
    make_source,
    mock_config,
)


def name_preview(root, alias, mode, depth, role="executor", index=0, audits=1):
    config = mock_config()
    config.agents[alias] = AgentConfig(cli_cmd=["mock"])
    config.agents["unused" + "x" * 300] = AgentConfig(cli_cmd=["mock"])
    workflow = literal_workflow()
    node = workflow.nodes[0]
    node.mode = "parallel" if mode == "parallel" else "sequential"
    node.depth = None if mode == "parallel" else depth
    node.providers = [
        ProviderSpec(provider="mock", role=role) for _ in range(index)
    ] + [ProviderSpec(provider=alias, role=role)]
    if mode == "review":
        if role == "executor":
            node.providers.append(ProviderSpec(provider="mock", role="reviewer"))
        else:
            node.providers.insert(0, ProviderSpec(provider="mock"))
        node.audit_rounds = audits
        config.settings.max_audit_rounds = audits
        node.review_starts_with = "reviewer"
    snapshot = build_runtime_config_snapshot(config, Console(), True).snapshot
    return compile_preflight_preview(
        make_source(workflow),
        config,
        snapshot,
        PreflightCompileOptions(
            project_root=root,
            state_dir=root / ".crewplane",
            fingerprint_key_policy="read_only",
        ),
    )


@pytest.mark.parametrize(
    ("mode", "depth", "role", "index"),
    [
        ("parallel", 1, "executor", 0),
        ("parallel", 1, "executor", 10),
        ("sequential", 9, "executor", 0),
        ("sequential", 10, "executor", 0),
        ("review", 8, "executor", 0),
        ("review", 9, "executor", 10),
        ("review", 8, "reviewer", 0),
        ("review", 9, "reviewer", 10),
    ],
)
def test_generated_component_boundary_is_shared_by_compilation_and_persisted_plan(
    tmp_path, mode, depth, role, index
):
    alias = boundary_alias(mode, depth, role, index)
    preview = name_preview(tmp_path, alias, mode, depth, role, index, audits=10)
    assert not preview.has_errors(), preview.diagnostics
    plan = PreflightExecutionPlan.from_preview(
        preview,
        "run",
        "demo-run",
        str(tmp_path),
        str(tmp_path / "run"),
        str(tmp_path / "run/manifests"),
        datetime(2026, 10, 26),
    )
    payload = plan.model_dump(mode="json")
    provider = next(
        record
        for record in payload["nodes"][0]["provider_records"]
        if record["provider"] == alias
    )
    provider["task_id"] = "a" + provider["task_id"]
    with pytest.raises(ValidationError, match="180-character budget"):
        PreflightExecutionPlan.model_validate(payload)
    rejected = name_preview(tmp_path, alias + "a", mode, depth, role, index, audits=10)
    assert rejected.has_errors()
    message = next(d.message for d in rejected.diagnostics if "budget" in d.message)
    assert (
        "build" in message
        and alias in message
        and "shorten the provider alias" in message
    )


@pytest.mark.parametrize("mode", ["sequential", "review"])
def test_round_digit_growth_rejects_previously_representable_alias(tmp_path, mode):
    depth = 8 if mode == "review" else 9
    alias = boundary_alias(mode, depth, "executor")
    assert not name_preview(tmp_path, alias, mode, depth).has_errors()
    assert name_preview(tmp_path, alias, mode, depth + 1).has_errors()
