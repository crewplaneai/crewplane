import hashlib
from pathlib import Path

from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION


def _config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
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
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content=prompt)
                ],
            )
        ],
    )
    config = _config()
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )
    return compile_preflight_preview(
        source=PreflightWorkflowSource.from_workflow(workflow),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            state_dir=root / ".crewplane",
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


def test_file_token_allows_allowlisted_external_path(
    tmp_path: Path,
) -> None:
    external_root = tmp_path / "external" / "shared-inputs"
    external_file = external_root / "context.md"
    external_file.parent.mkdir(parents=True)
    external_file.write_text("external context", encoding="utf-8")

    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content=f"{{{{file:{external_file.as_posix()}}}}}",
                    )
                ],
            )
        ],
    )
    config = _config()
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )

    preview = compile_preflight_preview(
        source=PreflightWorkflowSource.from_workflow(workflow),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=tmp_path / "project",
            state_dir=tmp_path / "project" / ".crewplane",
            fingerprint_key_policy="read_only",
            allowed_template_paths=(external_root,),
        ),
    )

    assert preview.diagnostics == []
    assert set(preview.static_file_payloads.values()) == {b"external context"}


def test_file_token_rejects_runtime_owned_crewplane_root(tmp_path: Path) -> None:
    runtime_file = tmp_path / ".crewplane" / "execution-stages" / "run" / "log.md"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("runtime", encoding="utf-8")

    preview = _compile_file_prompt(
        tmp_path,
        "{{file:.crewplane/execution-stages/run/log.md}}",
    )

    assert preview.workflow_signature is None
    assert [diagnostic.code for diagnostic in preview.diagnostics] == ["FILE-POLICY"]
    assert "runtime-owned path" in preview.diagnostics[0].message


def test_file_token_allows_user_authored_crewplane_inputs(tmp_path: Path) -> None:
    input_file = tmp_path / ".crewplane" / "inputs" / "context.md"
    input_file.parent.mkdir(parents=True)
    input_file.write_text("input context", encoding="utf-8")

    preview = _compile_file_prompt(tmp_path, "{{file:.crewplane/inputs/context.md}}")

    assert preview.diagnostics == []
    assert len(preview.static_resources) == 1
    assert set(preview.static_file_payloads.values()) == {b"input context"}
