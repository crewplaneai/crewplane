import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console

from crewplane.adapters.artifacts.terminal_history import (
    FilesystemTerminalHistoryReader,
)
from crewplane.architecture.ports import TerminalHistoryRead, TerminalHistoryReaderPort
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
from crewplane.core.preflight.compile_state import CompileState
from crewplane.core.preflight.diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from crewplane.core.preflight.models import StaticResource
from crewplane.core.preflight.static_resources import (
    append_static_resource,
    resolve_static_file,
    resolve_terminal_result_file,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.platforms import symlink_or_skip
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
                    options={"log_cli_output": True},
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
            terminal_history_reader=FilesystemTerminalHistoryReader(
                root / ".crewplane"
            ),
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


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_file_token_allows_terminal_execution_result_in_provider_prompt(
    tmp_path: Path,
    status: RunStatus,
) -> None:
    write_result_source(tmp_path, status=status)

    preview = _compile_file_prompt(tmp_path, RESULT_SOURCE_TOKEN)

    assert preview.diagnostics == []
    assert len(preview.static_resources) == 1
    assert set(preview.static_file_payloads.values()) == {b"prior result"}
    assert preview.render_plans[0].streams[0].fragments[0].kind == (
        "static_file_content"
    )


def test_file_token_rejects_running_execution_result_in_provider_prompt(
    tmp_path: Path,
) -> None:
    write_result_source(tmp_path, status="running")

    preview = _compile_file_prompt(tmp_path, RESULT_SOURCE_TOKEN)

    assert preview.workflow_signature is None
    assert [diagnostic.code for diagnostic in preview.diagnostics] == ["FILE-POLICY"]
    assert "still running" in preview.diagnostics[0].message


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
    external_path.write_text("external", encoding="utf-8", newline="\n")
    result_path.unlink()
    symlink_or_skip(result_path, external_path)

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


@pytest.mark.parametrize(
    ("raw_path", "payload", "code", "message", "has_resolved_path"),
    [
        (" \t", None, "FILE-POLICY", "Template file path is empty.", False),
        (" missing.md ", None, "FILE-POLICY", "File not found: missing.md", True),
        (
            "../outside.md",
            None,
            "FILE-POLICY",
            "Template access denied: ../outside.md",
            True,
        ),
        (
            ".crewplane/execution-stages/missing.md",
            None,
            "FILE-POLICY",
            "Template access denied for Crewplane runtime-owned path: .crewplane/execution-stages/missing.md",
            True,
        ),
        (
            "context.md",
            b"\xff",
            "FILE-ENCODING",
            "File token content must be UTF-8 text.",
            True,
        ),
        (
            "context.md",
            b"\x00\xff",
            "FILE-ENCODING",
            "File token content must be UTF-8 text.",
            True,
        ),
        (
            "context.md",
            b"text\x00text",
            "FILE-ENCODING",
            "File contains NUL bytes.",
            True,
        ),
    ],
)
def test_static_file_diagnostic_contract(
    tmp_path: Path,
    raw_path: str,
    payload: bytes | None,
    code: str,
    message: str,
    has_resolved_path: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    if payload is not None:
        (project / raw_path).write_bytes(payload)

    result = resolve_static_file(raw_path, project, project, (project,))

    metadata = (
        {"resolved_path": (project / raw_path.strip()).resolve().as_posix()}
        if has_resolved_path
        else {}
    )
    assert result.resource is None
    assert result.payload is None
    assert result.diagnostics == (
        PreflightDiagnostic(
            code=PreflightDiagnosticCode(code),
            phase=PreflightDiagnosticPhase.FILE_POLICY,
            path=raw_path.strip() if has_resolved_path else raw_path,
            message=message,
            metadata=metadata,
        ),
    )


@pytest.mark.parametrize("source", ["ordinary", "terminal"])
@pytest.mark.parametrize("payload", [b"", b"\xef\xbb\xbfcaf\xc3\xa9\r\n"])
def test_static_file_materialization_preserves_bytes_and_metadata(
    tmp_path: Path, source: str, payload: bytes
) -> None:
    path = tmp_path / "context.md"
    if source == "ordinary":
        path.write_bytes(payload)
        result = resolve_static_file(" context.md ", tmp_path, tmp_path, ())
    else:
        reader = Mock(spec=TerminalHistoryReaderPort)
        reader.read_terminal_result.return_value = TerminalHistoryRead(
            matched=True, path=path, payload=payload
        )
        result = resolve_terminal_result_file(" context.md ", tmp_path, reader)
        reader.read_terminal_result.assert_called_once_with("context.md", tmp_path)
        assert not path.exists()
        assert result is not None
        assert result.payload is payload

    digest = hashlib.sha256(payload).hexdigest()
    assert result.diagnostics == ()
    assert result.payload == payload
    assert result.resource == StaticResource(
        resource_id=digest,
        kind="file",
        raw_path="context.md",
        source_root=tmp_path.resolve().as_posix(),
        resolved_path=path.as_posix(),
        content_ref=f"static-files/{digest}.txt",
        size_bytes=len(payload),
        sha256=digest,
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\x00\xff", "File token content must be UTF-8 text."),
        (b"text\x00text", "File contains NUL bytes."),
    ],
)
def test_terminal_reader_bytes_preserve_encoding_diagnostic_precedence(
    tmp_path: Path, payload: bytes, message: str
) -> None:
    path = tmp_path / "missing-result.md"
    reader = Mock(spec=TerminalHistoryReaderPort)
    reader.read_terminal_result.return_value = TerminalHistoryRead(
        matched=True, path=path, payload=payload
    )

    result = resolve_terminal_result_file(" result.md ", tmp_path, reader)

    assert result is not None
    assert result.resource is None
    assert result.payload is None
    assert result.diagnostics == (
        PreflightDiagnostic(
            code=PreflightDiagnosticCode.FILE_ENCODING,
            phase=PreflightDiagnosticPhase.FILE_POLICY,
            path="result.md",
            message=message,
            metadata={"resolved_path": path.as_posix()},
        ),
    )


@pytest.mark.parametrize(
    ("operation", "strict"),
    [
        ("expanduser", None),
        ("resolve", False),
        ("exists", None),
        ("resolve", True),
        ("is_file", None),
        ("read_bytes", None),
    ],
)
def test_static_file_path_and_io_exceptions_propagate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    strict: bool | None,
) -> None:
    path = tmp_path / "context.md"
    path.write_bytes(b"content")
    original = getattr(Path, operation)
    error = OSError("Static file operation failed.")

    def fail(candidate: Path, *arguments: object, **options: object) -> object:
        if candidate == path and (
            operation != "resolve" or options.get("strict") == strict
        ):
            raise error
        return original(candidate, *arguments, **options)

    with monkeypatch.context() as patch:
        patch.setattr(Path, operation, fail)
        with pytest.raises(OSError) as raised:
            resolve_static_file(str(path), tmp_path, tmp_path, ())

    assert raised.value is error


@pytest.mark.parametrize("has_path", [False, True])
def test_terminal_reader_error_preserves_optional_path_metadata(
    tmp_path: Path, has_path: bool
) -> None:
    path = tmp_path / "result.md" if has_path else None
    reader = Mock(spec=TerminalHistoryReaderPort)
    reader.read_terminal_result.return_value = TerminalHistoryRead(
        matched=True, path=path, error="Terminal result unavailable."
    )

    result = resolve_terminal_result_file(" result.md ", tmp_path, reader)

    reader.read_terminal_result.assert_called_once_with("result.md", tmp_path)
    assert result is not None
    assert result.resource is None
    assert result.payload is None
    assert result.diagnostics == (
        PreflightDiagnostic(
            code=PreflightDiagnosticCode.FILE_POLICY,
            phase=PreflightDiagnosticPhase.FILE_POLICY,
            path="result.md",
            message="Terminal result unavailable.",
            metadata={"resolved_path": path.as_posix()} if path is not None else {},
        ),
    )


def test_terminal_reader_fallback_does_not_resolve_static_file(tmp_path: Path) -> None:
    reader = Mock(spec=TerminalHistoryReaderPort)
    reader.read_terminal_result.return_value = TerminalHistoryRead(matched=False)

    assert resolve_terminal_result_file(" missing.md ", tmp_path, None) is None
    assert resolve_terminal_result_file(" missing.md ", tmp_path, reader) is None
    reader.read_terminal_result.assert_called_once_with("missing.md", tmp_path)


def test_static_resource_registration_preserves_first_resource_and_payload(
    tmp_path: Path,
) -> None:
    for name, payload in (
        ("first.md", b"same"),
        ("second.md", b"same"),
        ("other.md", b"other"),
    ):
        (tmp_path / name).write_bytes(payload)
    first = resolve_static_file("first.md", tmp_path, tmp_path, ())
    second = resolve_static_file("second.md", tmp_path, tmp_path, ())
    other = resolve_static_file("other.md", tmp_path, tmp_path, ())
    assert first.resource is not None and first.payload is not None
    assert second.resource is not None and second.payload is not None
    assert other.resource is not None and other.payload is not None
    resource = first.resource.model_copy(update={"token_signatures": ["z"]})
    state = CompileState()

    append_static_resource(state, resource, first.payload, "z")
    append_static_resource(state, other.resource, other.payload, "other")
    append_static_resource(state, second.resource, second.payload, "a")
    append_static_resource(state, second.resource, second.payload, "a")

    assert state.static_resources == [
        resource.model_copy(update={"token_signatures": ["a", "z"]}),
        other.resource,
    ]
    assert resource.token_signatures == ["z"]
    assert state.static_resources[1] is other.resource
    assert list(state.static_payloads) == [
        resource.content_ref,
        other.resource.content_ref,
    ]
    assert state.static_payloads[resource.content_ref] is first.payload
