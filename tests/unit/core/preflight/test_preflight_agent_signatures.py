from __future__ import annotations

import json
from pathlib import Path

from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.version import SCHEMA_VERSION
from tests.unit.core.preflight.workspace_preflight_signatures_support import (
    compile_signature_workflow,
    project_root_workflow,
)


def test_unused_agent_config_does_not_change_workflow_signature(
    tmp_path: Path,
) -> None:
    workflow = project_root_workflow()
    first = compile_signature_workflow(
        tmp_path,
        workflow,
        two_agent_config(AgentConfig(cli_cmd=["unused-a"])),
    )
    second = compile_signature_workflow(
        tmp_path,
        workflow,
        two_agent_config(AgentConfig(cli_cmd=["unused-b"])),
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.workflow_signature is not None
    assert first.workflow_signature == second.workflow_signature
    assert (
        first.effective_runtime_config_signature
        == second.effective_runtime_config_signature
    )


def test_unused_agent_command_secret_does_not_require_fingerprints(
    tmp_path: Path,
) -> None:
    secret = "UNUSED_AGENT_SECRET"
    baseline = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        two_agent_config(AgentConfig(cli_cmd=["unused"])),
    )
    unused_secret = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        two_agent_config(AgentConfig(cli_cmd=["unused", "--api-key", secret])),
    )

    assert baseline.diagnostics == []
    assert unused_secret.diagnostics == []
    assert baseline.workflow_signature is not None
    assert baseline.workflow_signature == unused_secret.workflow_signature
    assert unused_secret.fingerprint_metadata["sensitive_values_required"] is False
    assert unused_secret.runtime_config_snapshot is not None
    assert unused_secret.runtime_config_snapshot.sensitive_config_paths == []
    assert secret not in unused_secret.model_dump_json()


def test_selected_agent_scalar_command_secrets_are_absent_from_compiled_plan(
    tmp_path: Path,
) -> None:
    model_arg_secret = "MODEL_ARG_SECRET"
    prompt_arg_secret = "PROMPT_ARG_SECRET"
    preview = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(
            AgentConfig(
                cli_cmd=["echo"],
                model_arg=f"--api-key={model_arg_secret}",
                prompt_transport_arg=f"--token={prompt_arg_secret}",
            )
        ),
    )

    serialized = json.dumps(preview.model_dump(mode="json"), sort_keys=True)

    assert preview.diagnostics == []
    assert preview.runtime_config_snapshot.sensitive_config_paths == [
        "agents.alpha.model_arg",
        "agents.alpha.prompt_transport_arg",
    ]
    assert model_arg_secret not in serialized
    assert prompt_arg_secret not in serialized


def test_selected_dotted_agent_command_secret_is_captured(tmp_path: Path) -> None:
    agent_name = "alpha.v2"
    secret = "DOTTED_AGENT_SECRET"
    preview = compile_signature_workflow(
        tmp_path,
        project_root_workflow(provider_name=agent_name),
        Config(
            version=SCHEMA_VERSION,
            agents={
                agent_name: AgentConfig(
                    cli_cmd=["echo", "--api-key", secret],
                )
            },
            settings=Settings(workspace={"enabled": False}),
        ),
    )

    assert preview.diagnostics == []
    assert preview.runtime_config_snapshot is not None
    sensitive_path = f"agents.{agent_name}.cli_cmd.2"
    assert preview.runtime_config_snapshot.sensitive_config_paths == [sensitive_path]
    assert preview.secret_context.get(f"config:{sensitive_path}") == secret


def test_explicit_provider_model_excludes_unused_default_model_from_signatures(
    tmp_path: Path,
) -> None:
    workflow = project_root_workflow(model="fixed-model")
    first = compile_signature_workflow(
        tmp_path,
        workflow,
        single_agent_config(AgentConfig(cli_cmd=["echo"], default_model="fallback-a")),
    )
    second = compile_signature_workflow(
        tmp_path,
        workflow,
        single_agent_config(AgentConfig(cli_cmd=["echo"], default_model="fallback-b")),
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.nodes[0].provider_records[0].model == "fixed-model"
    assert second.nodes[0].provider_records[0].model == "fixed-model"
    assert first.workflow_signature is not None
    assert first.workflow_signature == second.workflow_signature
    assert (
        first.effective_runtime_config_signature
        == second.effective_runtime_config_signature
    )


def test_inherited_default_model_changes_workflow_signature(tmp_path: Path) -> None:
    first = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(AgentConfig(cli_cmd=["echo"], default_model="model-a")),
    )
    second = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(AgentConfig(cli_cmd=["echo"], default_model="model-b")),
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.nodes[0].provider_records[0].model == "model-a"
    assert second.nodes[0].provider_records[0].model == "model-b"
    assert first.workflow_signature is not None
    assert first.workflow_signature != second.workflow_signature
    assert (
        first.effective_runtime_config_signature
        != second.effective_runtime_config_signature
    )


def test_non_generic_model_arg_excludes_ignored_config_field_from_signatures(
    tmp_path: Path,
) -> None:
    workflow = project_root_workflow(model="fixed-model")
    first = compile_signature_workflow(
        tmp_path,
        workflow,
        single_agent_config(
            AgentConfig(
                cli_cmd=["echo"],
                provider_kind="codex",
                model_arg="--ignored-a",
            )
        ),
    )
    second = compile_signature_workflow(
        tmp_path,
        workflow,
        single_agent_config(
            AgentConfig(
                cli_cmd=["echo"],
                provider_kind="codex",
                model_arg="--ignored-b",
            )
        ),
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.workflow_signature is not None
    assert first.workflow_signature == second.workflow_signature
    assert (
        first.effective_runtime_config_signature
        == second.effective_runtime_config_signature
    )


def test_generic_model_arg_without_resolved_model_does_not_change_signatures(
    tmp_path: Path,
) -> None:
    first = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(AgentConfig(cli_cmd=["echo"], model_arg="--model-a")),
    )
    second = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(AgentConfig(cli_cmd=["echo"], model_arg="--model-b")),
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.nodes[0].provider_records[0].model is None
    assert second.nodes[0].provider_records[0].model is None
    assert first.workflow_signature is not None
    assert first.workflow_signature == second.workflow_signature
    assert (
        first.effective_runtime_config_signature
        == second.effective_runtime_config_signature
    )


def test_generic_model_arg_with_resolved_model_changes_signatures(
    tmp_path: Path,
) -> None:
    workflow = project_root_workflow(model="fixed-model")
    first = compile_signature_workflow(
        tmp_path,
        workflow,
        single_agent_config(AgentConfig(cli_cmd=["echo"], model_arg="--model-a")),
    )
    second = compile_signature_workflow(
        tmp_path,
        workflow,
        single_agent_config(AgentConfig(cli_cmd=["echo"], model_arg="--model-b")),
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.nodes[0].provider_records[0].model == "fixed-model"
    assert second.nodes[0].provider_records[0].model == "fixed-model"
    assert first.workflow_signature is not None
    assert first.workflow_signature != second.workflow_signature
    assert (
        first.effective_runtime_config_signature
        != second.effective_runtime_config_signature
    )


def test_retry_delay_without_configured_retries_does_not_change_signatures(
    tmp_path: Path,
) -> None:
    first = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(
            AgentConfig(
                cli_cmd=["echo"],
                max_retries=0,
                retry_delay_seconds=1.0,
                retry_on_exit_codes=[1],
            )
        ),
    )
    second = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(
            AgentConfig(
                cli_cmd=["echo"],
                max_retries=0,
                retry_delay_seconds=999.0,
                retry_on_exit_codes=[1],
            )
        ),
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.workflow_signature is not None
    assert first.workflow_signature == second.workflow_signature
    assert (
        first.effective_runtime_config_signature
        == second.effective_runtime_config_signature
    )


def test_retry_settings_without_matchers_do_not_change_signatures(
    tmp_path: Path,
) -> None:
    first = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(
            AgentConfig(
                cli_cmd=["echo"],
                max_retries=1,
                retry_delay_seconds=1.0,
            )
        ),
    )
    second = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(
            AgentConfig(
                cli_cmd=["echo"],
                max_retries=2,
                retry_delay_seconds=999.0,
            )
        ),
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.workflow_signature is not None
    assert first.workflow_signature == second.workflow_signature
    assert (
        first.effective_runtime_config_signature
        == second.effective_runtime_config_signature
    )


def test_retry_delay_with_configured_retries_changes_signatures(
    tmp_path: Path,
) -> None:
    first = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(
            AgentConfig(
                cli_cmd=["echo"],
                max_retries=1,
                retry_delay_seconds=1.0,
                retry_on_exit_codes=[1],
            )
        ),
    )
    second = compile_signature_workflow(
        tmp_path,
        project_root_workflow(),
        single_agent_config(
            AgentConfig(
                cli_cmd=["echo"],
                max_retries=1,
                retry_delay_seconds=999.0,
                retry_on_exit_codes=[1],
            )
        ),
    )

    assert first.diagnostics == []
    assert second.diagnostics == []
    assert first.workflow_signature is not None
    assert first.workflow_signature != second.workflow_signature
    assert (
        first.effective_runtime_config_signature
        != second.effective_runtime_config_signature
    )


def two_agent_config(beta: AgentConfig) -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={
            "alpha": AgentConfig(cli_cmd=["echo"]),
            "beta": beta,
        },
        settings=Settings(workspace={"enabled": False}),
    )


def single_agent_config(alpha: AgentConfig) -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": alpha},
        settings=Settings(workspace={"enabled": False}),
    )
