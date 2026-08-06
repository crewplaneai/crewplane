import hashlib
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from crewplane.core.execution_state import RunStatus
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
    load_workflow_source_for_preflight,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.terminal_results import (
    FINDINGS_SOURCE_TOKEN,
    RESULT_SOURCE_TOKEN,
    write_result_source,
)


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
    return _compile_source(root, PreflightWorkflowSource.from_workflow(workflow))


def _compile_source(root: Path, source: PreflightWorkflowSource):
    config = _config()
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )
    return compile_preflight_preview(
        source=source,
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            state_dir=root / ".crewplane",
            fingerprint_key_policy="read_only",
        ),
    )


def _compile_input_source(root: Path, source: str):
    workflow = WorkflowPlan(
        name="demo",
        nodes=[WorkflowNode(id="context", mode="input", source=source)],
    )
    return _compile_source(root, PreflightWorkflowSource.from_workflow(workflow))


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


def test_file_token_rejects_execution_result_in_provider_prompt(
    tmp_path: Path,
) -> None:
    write_result_source(tmp_path)

    preview = _compile_file_prompt(tmp_path, RESULT_SOURCE_TOKEN)

    assert preview.workflow_signature is None
    assert [diagnostic.code for diagnostic in preview.diagnostics] == ["FILE-POLICY"]
    assert "runtime-owned path" in preview.diagnostics[0].message


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_input_node_allows_terminal_execution_result_as_static_resource(
    tmp_path: Path,
    status: RunStatus,
) -> None:
    write_result_source(tmp_path, status=status)

    preview = _compile_input_source(tmp_path, RESULT_SOURCE_TOKEN)

    assert preview.diagnostics == []
    assert len(preview.static_resources) == 1
    resource = preview.static_resources[0]
    assert preview.nodes[0].input_content_ref == resource.content_ref
    assert set(preview.static_file_payloads.values()) == {b"prior result"}


def test_input_node_rejects_running_execution_result(tmp_path: Path) -> None:
    write_result_source(tmp_path, status="running")

    preview = _compile_input_source(tmp_path, RESULT_SOURCE_TOKEN)

    assert preview.workflow_signature is None
    assert [diagnostic.code for diagnostic in preview.diagnostics] == ["FILE-POLICY"]
    assert "still running" in preview.diagnostics[0].message


@pytest.mark.parametrize("manifest_payload", [None, "not-json"])
def test_input_node_rejects_result_without_valid_run_manifest(
    tmp_path: Path,
    manifest_payload: str | None,
) -> None:
    write_result_source(tmp_path)
    manifest_path = (
        tmp_path
        / ".crewplane"
        / "execution-stages"
        / "workflow--prior-run"
        / "manifests"
        / "run.json"
    )
    if manifest_payload is None:
        manifest_path.unlink()
    else:
        manifest_path.write_text(manifest_payload, encoding="utf-8")

    preview = _compile_input_source(tmp_path, RESULT_SOURCE_TOKEN)

    assert preview.workflow_signature is None
    assert [diagnostic.code for diagnostic in preview.diagnostics] == ["FILE-POLICY"]
    assert "manifest" in preview.diagnostics[0].message


def test_input_node_rejects_symlinked_execution_result(tmp_path: Path) -> None:
    result_path = write_result_source(tmp_path)
    external_path = tmp_path / "external-result.md"
    external_path.write_text("external", encoding="utf-8")
    result_path.unlink()
    result_path.symlink_to(external_path)

    preview = _compile_input_source(tmp_path, RESULT_SOURCE_TOKEN)

    assert preview.workflow_signature is None
    assert [diagnostic.code for diagnostic in preview.diagnostics] == ["FILE-POLICY"]
    assert "safe regular file" in preview.diagnostics[0].message


def test_input_node_allows_terminal_findings_as_static_resource(
    tmp_path: Path,
) -> None:
    write_result_source(
        tmp_path,
        content=b"prior findings",
        artifact_kind="findings",
    )

    preview = _compile_input_source(tmp_path, FINDINGS_SOURCE_TOKEN)

    assert preview.diagnostics == []
    assert set(preview.static_file_payloads.values()) == {b"prior findings"}


def test_imported_input_node_resolves_terminal_result_from_project_root(
    tmp_path: Path,
) -> None:
    write_result_source(tmp_path)
    module_path = tmp_path / "module.task.md"
    module_path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Result Consumer",
                "nodes:",
                "  - id: context",
                "    mode: input",
                f'    source: "{RESULT_SOURCE_TOKEN}"',
                "  - id: use-context",
                "    mode: sequential",
                "    needs: [context]",
                "    providers: [alpha]",
                "---",
                "",
                "## use-context",
                "",
                "Use {{context.output}}.",
            ]
        ),
        encoding="utf-8",
    )
    root_path = tmp_path / "root.task.md"
    root_path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Root",
                "imports:",
                "  - path: module.task.md",
                "    as: archive",
                "nodes: []",
                "---",
            ]
        ),
        encoding="utf-8",
    )
    source = load_workflow_source_for_preflight(root_path, project_root=tmp_path)

    preview = _compile_source(tmp_path, source)

    assert preview.diagnostics == []
    input_node = next(node for node in preview.nodes if node.id == "archive.context")
    assert input_node.input_content_ref is not None
    assert input_node.source_file == module_path.as_posix()
    assert input_node.source_root == tmp_path.as_posix()
    assert [record.path for record in source.referenced_workflows] == [
        root_path.resolve(),
        module_path.resolve(),
    ]


def test_input_node_rejects_other_runtime_owned_path(tmp_path: Path) -> None:
    runtime_file = tmp_path / ".crewplane" / "execution-stages" / "run" / "log.md"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("runtime", encoding="utf-8")

    preview = _compile_input_source(
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
