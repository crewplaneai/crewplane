import hashlib
from pathlib import Path

from rich.console import Console

from orchestrator_cli.bootstrap import build_runtime_config_snapshot
from orchestrator_cli.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from orchestrator_cli.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from orchestrator_cli.core.prompt_segments import PromptSegment
from orchestrator_cli.core.workflow_models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.versions import CONFIG_SCHEMA_VERSION


def _config() -> Config:
    return Config(
        version=CONFIG_SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"])},
        settings=Settings(
            integrations=IntegrationsConfig(
                invoker=IntegrationSpec(
                    implementation="mock",
                    options={"output_mode": "echo"},
                ),
                artifacts=IntegrationSpec(
                    implementation="filesystem",
                    options={"allowed_template_paths": [], "log_cli_output": True},
                ),
                ui=IntegrationSpec(implementation="none", options={}),
            )
        ),
    )


def _compile_file_prompt(root: Path, prompt: str):
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content=prompt)],
            )
        ],
    )
    config = _config()
    snapshot = build_runtime_config_snapshot(
        config=config,
        workflow_schema_version=workflow.schema_version,
        console=Console(file=None),
        no_live=True,
    )
    return compile_preflight_preview(
        source=PreflightWorkflowSource.from_workflow(workflow),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            orchestrator_dir=root / ".orchestrator",
            fingerprint_key_policy="read_only",
        ),
    )


def test_file_token_is_materialized_as_static_resource(tmp_path: Path) -> None:
    (tmp_path / "context.md").write_text("static context", encoding="utf-8")

    preview = _compile_file_prompt(tmp_path, "{{file:context.md}}")

    assert not preview.diagnostics
    assert len(preview.static_resources) == 1
    resource = preview.static_resources[0]
    content_sha256 = hashlib.sha256(b"static context").hexdigest()
    assert resource.resource_id == content_sha256
    assert resource.content_ref == f"static-files/{content_sha256}.txt"
    assert resource.sha256 == content_sha256
    assert len(resource.token_signatures) == 2
    assert set(preview.static_file_payloads.values()) == {b"static context"}
    assert preview.render_plans[0].streams[0].fragments[0].kind == "static_file_content"


def test_same_file_content_uses_one_content_addressed_static_resource(
    tmp_path: Path,
) -> None:
    (tmp_path / "first.md").write_text("same content", encoding="utf-8")
    (tmp_path / "second.md").write_text("same content", encoding="utf-8")

    preview = _compile_file_prompt(
        tmp_path,
        "{{file:first.md}}\n{{file:second.md}}",
    )

    content_sha256 = hashlib.sha256(b"same content").hexdigest()
    assert not preview.diagnostics
    assert [resource.content_ref for resource in preview.static_resources] == [
        f"static-files/{content_sha256}.txt"
    ]
    assert list(preview.static_file_payloads) == [f"static-files/{content_sha256}.txt"]
    assert len(preview.static_resources[0].token_signatures) == 4
    assert len(preview.token_catalog) == 4


def test_non_utf8_file_token_fails_in_preflight(tmp_path: Path) -> None:
    (tmp_path / "payload.bin").write_bytes(b"\xff\xfe")

    preview = _compile_file_prompt(tmp_path, "{{file:payload.bin}}")

    assert preview.workflow_signature is None
    assert [diagnostic.code for diagnostic in preview.diagnostics] == ["FILE-ENCODING"]
