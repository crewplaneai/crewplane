import hashlib
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.preflight.compiled_records import (
    Fragment,
    RenderPlan,
    RenderStream,
    WorkspaceFileLocator,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.common import CompiledRuntimeContext
from crewplane.runtime.execution.fragment_assembler import assemble_prompt_details
from crewplane.runtime.execution.input import InputContentError, execute_input_stage
from crewplane.runtime.execution.preflight_files import read_verified_static_text
from crewplane.runtime.execution.workspace_files import (
    resolve_project_initial_workspace_file,
)
from tests.helpers.platforms import symlink_or_skip
from tests.helpers.resume import make_plan
from tests.helpers.static_resources import static_resource


@pytest.fixture(params=["input", "static", "workspace"])
def content_reader(request, tmp_path):
    plan = make_plan().model_copy(update={"context_root": str(tmp_path)})
    output = OutputManager("Workflow", base_dir=tmp_path / "artifacts")
    secrets = SecretContext()

    def read(reference, payload=b"outside"):
        resource = static_resource(payload).model_copy(
            update={"content_ref": reference}
        )
        compiled = plan.model_copy(update={"static_resources": [resource]})
        if request.param == "input":
            node = compiled.nodes[0].model_copy(
                update={"mode": "input", "input_content_ref": reference}
            )
            execute_input_stage(node, output, CompiledRuntimeContext(compiled, secrets))
            return (
                (output.stages_dir / "a/input_round1.md").read_bytes().decode("utf-8")
            )
        if request.param == "static":
            render_plan = RenderPlan(
                render_plan_id="a",
                node_id="a",
                streams=[
                    RenderStream(
                        target_role=ProviderRole.EXECUTOR,
                        fragments=[
                            Fragment(
                                fragment_index=0,
                                kind="static_file_content",
                                source_role=PromptSegmentRole.SHARED,
                                content_ref=reference,
                            )
                        ],
                    )
                ],
            )
            compiled = compiled.model_copy(update={"render_plans": [render_plan]})
            return assemble_prompt_details(
                compiled, compiled.nodes[0], ProviderRole.EXECUTOR, output, secrets
            ).text
        locator = WorkspaceFileLocator.model_construct(
            locator_id="workspace-file-test",
            content_ref=reference,
            occurrence_id="a:executor:0:file:context.txt",
            node_id="a",
            target="executor_prompt",
            source_class="project_initial",
            raw_token="{{file:context.txt}}",
            raw_path="context.txt",
            source_root=str(tmp_path),
            source_root_relative_to_project=".",
            project_root_relative_to_git_top=".",
            git_top_relative_path="context.txt",
            workspace_relative_path="context.txt",
        )
        compiled = plan.model_copy(update={"workspace_file_locators": [locator]})
        return resolve_project_initial_workspace_file(compiled, locator.locator_id)

    return request.param, read


@pytest.mark.parametrize(
    "reference", ["../outside.txt", "nested/../file.txt", "absolute"]
)
def test_invalid_reference_is_rejected_with_consumer_context(
    tmp_path, content_reader, reference
):
    kind, read = content_reader
    if reference == "absolute":
        reference = str(tmp_path / "outside.txt")

    with pytest.raises(InputContentError if kind == "input" else ValueError) as caught:
        read(reference)

    if kind == "workspace":
        assert (
            str(caught.value) == f"Invalid workspace content reference '{reference}'."
        )
        assert caught.value.__cause__ is None
    else:
        assert "missing or unsafe" in str(caught.value)
        assert reference in str(caught.value)
        assert "node 'a'" in str(caught.value).lower()


@pytest.mark.parametrize(
    "reference", ["", ".", "missing.txt", "directory", "symlink", "hardlink"]
)
def test_unsafe_or_missing_content_keeps_caller_error_policy(
    tmp_path, content_reader, reference
):
    kind, read = content_reader
    preflight = tmp_path / "preflight"
    preflight.mkdir()
    if reference == "directory":
        (preflight / reference).mkdir()
    elif reference in {"symlink", "hardlink"}:
        source = tmp_path / "outside.txt"
        source.write_bytes(b"outside")
        target = preflight / reference
        if reference == "symlink":
            symlink_or_skip(target, source)
        else:
            target.hardlink_to(source)
    error_type = (
        InputContentError
        if kind == "input"
        else RuntimeError
        if kind == "workspace"
        else ValueError
    )

    with pytest.raises(error_type) as caught:
        read(reference)

    if kind == "workspace":
        assert (
            str(caught.value)
            == f"Workspace content reference is missing or unsafe: '{reference}'."
        )
        assert caught.value.__cause__ is None
    else:
        assert "missing or unsafe" in str(caught.value)
        assert "node 'a'" in str(caught.value).lower()


@pytest.mark.parametrize(
    "payload,error",
    [
        (b"", None),
        (b"first\r\nsecond\r\n", None),
        ("café 🐍\r\n".encode(), None),
        (b"\xef\xbb\xbftext", None),
        (b"text\x00", "nul"),
        (b"\xff", "utf8"),
        (b"\xe2\x82", "utf8"),
        (b"\x00\xff", "utf8"),
    ],
)
def test_content_readers_preserve_text_and_byte_semantics(
    tmp_path, content_reader, payload, error
):
    kind, read = content_reader
    path = tmp_path / "preflight" / "content.txt"
    path.parent.mkdir()
    path.write_bytes(payload)

    if error is None:
        if kind == "workspace":
            resolved = read("./content.txt")
            assert resolved.text.encode() == payload
            assert resolved.byte_size == len(payload)
            assert resolved.sha256 == hashlib.sha256(payload).hexdigest()
        elif kind == "input" and not payload:
            with pytest.raises(InputContentError) as caught:
                read("content.txt", payload)
            assert str(caught.value) == (
                "Resolved input content for node 'a' is empty after preflight assembly."
            )
            assert caught.value.__cause__ is None
        else:
            assert read("content.txt", payload).encode() == payload
        return

    if kind == "workspace":
        with pytest.raises(RuntimeError) as caught:
            read("./content.txt")
        detail = "is not valid UTF-8" if error == "utf8" else "contains NUL bytes"
        assert str(caught.value) == (
            f"Workspace file locator content {detail}: workspace-file-test."
        )
        if error == "utf8":
            assert isinstance(caught.value.__cause__, UnicodeDecodeError)
            assert caught.value.__cause__.object == payload
        else:
            assert caught.value.__cause__ is None
        return

    with pytest.raises(InputContentError if kind == "input" else ValueError) as caught:
        read("content.txt", payload)
    prefix = "Input node 'a'" if kind == "input" else "Node 'a'"
    detail = "is not valid UTF-8" if error == "utf8" else "contains NUL content"
    inner_message = f"Static resource 'content.txt' {detail}."
    assert str(caught.value) == f"{prefix}: {inner_message}"
    inner = caught.value.__cause__
    assert type(inner) is ValueError
    assert str(inner) == inner_message
    assert inner.__cause__ is None
    if error == "utf8":
        assert inner.__suppress_context__
        assert isinstance(inner.__context__, UnicodeDecodeError)
        assert inner.__context__.object == payload
    else:
        assert inner.__context__ is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", None),
        ("sha256", False),
        ("sha256", 0),
        ("sha256", {}),
        ("sha256", b"0" * 64),
        ("sha256", "A" * 64),
        ("sha256", "0" * 63),
        ("sha256", "0" * 65),
        ("sha256", "g" * 64),
        ("sha256", "0" * 64 + "\n"),
        ("encoding", "UTF-8"),
        ("encoding", None),
        ("size_bytes", -1),
        ("size_bytes", False),
        ("size_bytes", True),
        ("size_bytes", 0.0),
        ("size_bytes", "4"),
        ("size_bytes", None),
    ],
)
def test_static_resource_metadata_rejection_precedes_file_access(
    tmp_path: Path, field: str, value: object
) -> None:
    resource = static_resource(b"text", "missing.txt").model_copy(update={field: value})
    plan = make_plan().model_copy(
        update={"context_root": str(tmp_path), "static_resources": [resource]}
    )

    with pytest.raises(ValueError) as caught:
        read_verified_static_text(plan, "missing.txt")

    assert str(caught.value) == (
        "Static resource 'missing.txt' has invalid encoding, size, or SHA-256 metadata."
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not (tmp_path / "preflight").exists()


@pytest.mark.parametrize("descriptor_count", [0, 2])
def test_static_resource_cardinality_precedes_metadata_and_file_access(
    tmp_path: Path, descriptor_count: int
) -> None:
    resource = static_resource(b"text", "missing.txt").model_copy(
        update={"sha256": None}
    )
    plan = make_plan().model_copy(
        update={
            "context_root": str(tmp_path),
            "static_resources": [resource] * descriptor_count,
        }
    )

    with pytest.raises(ValueError) as caught:
        read_verified_static_text(plan, "missing.txt")

    assert (
        str(caught.value)
        == "Static resource 'missing.txt' requires exactly one descriptor."
    )
    assert caught.value.__cause__ is None
    assert not (tmp_path / "preflight").exists()


@pytest.mark.parametrize("descriptor_payload", [b"valid", b"ok"])
def test_static_resource_integrity_rejection_precedes_decoding(
    tmp_path: Path, descriptor_payload: bytes
) -> None:
    payload = b"\x00\xff"
    path = tmp_path / "preflight" / "content.txt"
    path.parent.mkdir()
    path.write_bytes(payload)
    resource = static_resource(descriptor_payload, "content.txt")
    plan = make_plan().model_copy(
        update={"context_root": str(tmp_path), "static_resources": [resource]}
    )

    with pytest.raises(ValueError) as caught:
        read_verified_static_text(plan, "content.txt")

    assert (
        str(caught.value)
        == "Static resource 'content.txt' bytes do not match its descriptor."
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert path.read_bytes() == payload
