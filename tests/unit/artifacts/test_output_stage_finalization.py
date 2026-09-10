from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import patch

from crewplane.architecture.contracts import (
    build_result_filename,
)
from crewplane.artifacts import OutputManager
from crewplane.artifacts.generated_files.catalog import (
    snapshot_generated_file_workspace,
)
from tests.helpers.artifacts import node_artifact_request


def test_finalize_stage_consolidates_task_outputs(tmp_path: Path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    (stage_dir / "alpha_round1.md").write_text("alpha", encoding="utf-8")
    (stage_dir / "beta_round1.md").write_text("beta", encoding="utf-8")

    result = output.finalize_node(node_artifact_request("build.node"))

    result_text = (output.results_dir / build_result_filename("build.node")).read_text(
        encoding="utf-8"
    )
    assert result.stage_name == "build.node"
    assert "alpha" in result_text
    assert "beta" in result_text


def test_finalize_stage_links_generated_files_against_project_root(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    (base_dir / "src").mkdir()
    (base_dir / "src" / "app.txt").write_text("content", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    (stage_dir / "alpha_round1.md").write_text(
        "Updated `src/app.txt`.\n",
        encoding="utf-8",
    )

    output.finalize_node(node_artifact_request("build.node"))

    result_text = (output.results_dir / build_result_filename("build.node")).read_text(
        encoding="utf-8"
    )
    assert "## Generated Files" in result_text
    assert "[src/app.txt]" in result_text


def test_finalize_stage_namespaces_workspace_generated_files_by_task(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    alpha_workspace = base_dir / "alpha-workspace"
    beta_workspace = base_dir / "beta-workspace"
    (alpha_workspace / "src").mkdir(parents=True)
    (beta_workspace / "src").mkdir(parents=True)
    (alpha_workspace / "src" / "app.txt").write_text(
        "alpha content",
        encoding="utf-8",
    )
    (alpha_workspace / "src" / "app.txt").chmod(0o640)
    (beta_workspace / "src" / "app.txt").write_text(
        "beta content",
        encoding="utf-8",
    )
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    beta_output = stage_dir / "beta_round1.md"
    alpha_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")
    beta_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")

    result = output.finalize_node(
        node_artifact_request("build.node"),
        generated_file_workspace_roots={
            alpha_output.resolve(strict=False): alpha_workspace,
            beta_output.resolve(strict=False): beta_workspace,
        },
    )

    result_text = (output.results_dir / build_result_filename("build.node")).read_text(
        encoding="utf-8"
    )
    assert "[alpha/src/app.txt]" in result_text
    assert "[beta/src/app.txt]" in result_text
    assert len(result.generated_files) == 2
    generated_dir = output.results_dir / "generated-files" / "build.node"
    assert (generated_dir / "alpha" / "src" / "app.txt").read_text(
        encoding="utf-8"
    ) == "alpha content"
    assert (generated_dir / "beta" / "src" / "app.txt").read_text(
        encoding="utf-8"
    ) == "beta content"
    assert (
        stat.S_IMODE((generated_dir / "alpha" / "src" / "app.txt").stat().st_mode)
        == 416
    )


def test_finalize_stage_preserves_result_when_generated_file_copy_fails(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "blocked").mkdir(parents=True)
    (workspace / "blocked" / "app.txt").write_text(
        "blocked content",
        encoding="utf-8",
    )
    (workspace / "good.txt").write_text("good content", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    provider_output = stage_dir / "alpha_round1.md"
    provider_output.write_text(
        "## Generated Files\n\n- `blocked/app.txt`\n- `good.txt`\n",
        encoding="utf-8",
    )
    snapshot = snapshot_generated_file_workspace(provider_output, workspace)
    blocking_path = (
        output.results_dir / "generated-files" / "build.node" / "alpha" / "blocked"
    )
    blocking_path.parent.mkdir(parents=True, exist_ok=True)
    blocking_path.write_text("preserve me", encoding="utf-8")

    result = output.finalize_node(
        node_artifact_request("build.node"),
        generated_file_workspace_roots={
            provider_output.resolve(strict=False): snapshot,
        },
    )

    result_text = result.result_file.read_text(encoding="utf-8")
    generated_file = (
        output.results_dir / "generated-files" / "build.node" / "alpha" / "good.txt"
    )
    assert provider_output.is_file()
    assert "[alpha/good.txt]" in result_text
    assert "[alpha/blocked/app.txt]" not in result_text
    assert result.generated_files == (generated_file,)
    assert generated_file.read_text(encoding="utf-8") == "good content"
    assert blocking_path.read_text(encoding="utf-8") == "preserve me"
    assert len(result.warnings) == 1
    assert "alpha/blocked/app.txt" in result.warnings[0]
    assert "copy failed" in result.warnings[0].lower()


def test_finalize_stage_removes_partial_generated_file_copy(tmp_path: Path) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    workspace.mkdir()
    (workspace / "app.txt").write_text("complete", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    provider_output = stage_dir / "alpha_round1.md"
    provider_output.write_text(
        "## Generated Files\n\n- `app.txt`\n",
        encoding="utf-8",
    )
    snapshot = snapshot_generated_file_workspace(provider_output, workspace)

    def fail_after_partial_copy(source: Path, target: Path) -> None:  # noqa: ARG001
        Path(target).write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    with patch(
        "crewplane.artifacts.generated_files.catalog.shutil.copyfile",
        side_effect=fail_after_partial_copy,
    ):
        result = output.finalize_node(
            node_artifact_request("build.node"),
            generated_file_workspace_roots={
                provider_output.resolve(strict=False): snapshot,
            },
        )

    result_text = result.result_file.read_text(encoding="utf-8")
    generated_file_dir = output.results_dir / "generated-files" / "build.node" / "alpha"
    assert "[alpha/app.txt]" not in result_text
    assert result.generated_files == ()
    assert len(result.warnings) == 1
    assert "disk full" in result.warnings[0]
    assert not (generated_file_dir / "app.txt").exists()
    assert list(generated_file_dir.glob(".generated-file-*.tmp")) == []


def test_finalize_stage_can_disable_generated_file_detection(tmp_path: Path) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    (base_dir / "src").mkdir()
    (base_dir / "src" / "app.txt").write_text("stale", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    (stage_dir / "alpha_round1.md").write_text(
        "Updated `src/app.txt`.\n",
        encoding="utf-8",
    )

    output.finalize_node(
        node_artifact_request("build.node"),
        generated_file_detection_enabled=False,
    )

    result_text = (output.results_dir / build_result_filename("build.node")).read_text(
        encoding="utf-8"
    )
    assert "## Generated Files" not in result_text
    assert "Updated `src/app.txt`." in result_text
