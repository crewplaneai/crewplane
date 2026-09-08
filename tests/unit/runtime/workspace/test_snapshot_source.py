from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)

isolated_git = isolated_git_support.isolated_git


@pytest.mark.parametrize("project_relative", (".", "nested"))
@pytest.mark.parametrize("retry", (False, True), ids=("initial", "retry"))
def test_snapshot_preserves_source_bytes_after_live_attributes_change(
    tmp_path: Path,
    isolated_git: IsolatedGit,
    project_relative: str,
    retry: bool,
) -> None:
    repo = create_git_repo(tmp_path)
    project_root = repo / project_relative
    project_root.mkdir(exist_ok=True)
    source_bytes = b"line one\nline two\n"
    (project_root / "app.txt").write_bytes(source_bytes)
    isolated_git.run_text(repo, "add", ".")
    isolated_git.run_text(repo, "commit", "-m", "record snapshot source")
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    source = plan.workspace_source
    assert source is not None
    plan = plan.model_copy(
        update={
            "project_root": project_root.as_posix(),
            "workspace_source": source.model_copy(
                update={"project_root_relative_path": project_relative}
            ),
        }
    )
    output = workspace_output_manager(tmp_path, project_root)
    output.create_node_dir(node_artifact_request("implement"))
    live_attributes = repo / ".gitattributes"
    if not retry:
        live_attributes.write_text("*.txt text eol=crlf\n", encoding="utf-8")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    if retry:
        live_attributes.write_text("*.txt text eol=crlf\n", encoding="utf-8")
        (prepared.cwd / "app.txt").write_bytes(b"partial attempt\n")
        retry_reset = prepared.invocation_context.retry_reset
        assert retry_reset is not None
        retry_reset()

    assert (prepared.cwd / "app.txt").read_bytes() == source_bytes
    assert (project_root / "app.txt").read_bytes() == source_bytes
    prepared.mark_succeeded()
    assert prepared.state_path is not None
    state = read_json_object(prepared.state_path)
    assert state["status"] == "succeeded"
    assert state["result"]["changed_path_count"] == 0
    assert prepared.workspace_path is not None
    assert not prepared.workspace_path.exists()
