import hashlib

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.preflight.models import (
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


@pytest.mark.parametrize("payload", [b"first\r\nsecond\r\n", b"text\x00", b"\xff"])
def test_content_readers_preserve_text_and_byte_semantics(
    tmp_path, content_reader, payload
):
    kind, read = content_reader
    path = tmp_path / "preflight" / "content.txt"
    path.parent.mkdir()
    path.write_bytes(payload)

    if kind == "workspace" and payload == b"\xff":
        with pytest.raises(RuntimeError, match="not valid UTF-8") as caught:
            read("./content.txt")
        assert isinstance(caught.value.__cause__, UnicodeDecodeError)
    elif kind == "workspace" and payload == b"text\x00":
        with pytest.raises(RuntimeError, match="contains NUL bytes"):
            read("./content.txt")
    elif kind == "workspace":
        resolved = read("./content.txt")
        assert resolved.text == payload.decode()
        assert resolved.byte_size == len(payload)
        assert resolved.sha256 == hashlib.sha256(payload).hexdigest()
    elif payload in (b"\xff", b"text\x00"):
        message = "not valid UTF-8" if payload == b"\xff" else "contains NUL"
        with pytest.raises(
            InputContentError if kind == "input" else ValueError, match=message
        ):
            read("content.txt", payload)
    else:
        assert read("content.txt", payload).encode() == payload
