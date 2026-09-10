from __future__ import annotations

import json
import shutil
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


def test_workspace_generated_files_hash_truncated_stage_directories(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    stage_prefix = "a" * 120
    generated_paths = []
    for suffix in ("x", "y"):
        stage_name = f"{stage_prefix}{suffix}"
        workspace = base_dir / f"workspace-{suffix}"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "app.txt").write_text(
            f"{suffix} content",
            encoding="utf-8",
        )
        stage_dir = output.create_node_dir(node_artifact_request(stage_name))
        provider_output = stage_dir / "alpha_round1.md"
        provider_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")

        result = output.finalize_node(
            node_artifact_request(stage_name),
            generated_file_workspace_roots={
                provider_output.resolve(strict=False): workspace,
            },
        )
        generated_paths.extend(result.generated_files)

    assert len(generated_paths) == 2
    assert generated_paths[0].parent != generated_paths[1].parent
    assert generated_paths[0].read_text(encoding="utf-8") == "x content"
    assert generated_paths[1].read_text(encoding="utf-8") == "y content"


def test_workspace_generated_files_use_snapshot_not_mutated_workspace(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    generated_file = workspace / "src" / "app.txt"
    generated_file.write_text("original", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text("Updated `src/app.txt`.\n", encoding="utf-8")
    snapshot = snapshot_generated_file_workspace(alpha_output, workspace)
    generated_file.write_text("mutated", encoding="utf-8")

    output.finalize_node(
        node_artifact_request("build.node"),
        generated_file_workspace_roots={
            alpha_output.resolve(strict=False): snapshot,
        },
    )

    generated_dir = output.results_dir / "generated-files" / "build.node"
    assert (generated_dir / "alpha" / "src" / "app.txt").read_text(
        encoding="utf-8"
    ) == "original"


def test_workspace_generated_files_resolve_original_absolute_paths_from_snapshot(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    generated_file = workspace / "src" / "app.txt"
    generated_file.write_text("original", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text(
        f"Updated `{generated_file.as_posix()}`.\n",
        encoding="utf-8",
    )
    snapshot = snapshot_generated_file_workspace(alpha_output, workspace)
    shutil.rmtree(workspace)

    result = output.finalize_node(
        node_artifact_request("build.node"),
        generated_file_workspace_roots={
            alpha_output.resolve(strict=False): snapshot,
        },
    )

    generated_dir = output.results_dir / "generated-files" / "build.node"
    generated_result = generated_dir / "alpha" / "src" / "app.txt"
    assert generated_result.read_text(encoding="utf-8") == "original"
    assert result.generated_files == (generated_result,)


def test_workspace_generated_file_snapshot_skips_unchanged_claims(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "documented.txt").write_text(
        "same bytes",
        encoding="utf-8",
    )
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text(
        "Updated `src/documented.txt`.\n",
        encoding="utf-8",
    )

    snapshot = snapshot_generated_file_workspace(
        alpha_output,
        workspace,
        changed_paths=set(),
    )
    snapshot_metadata = json.loads(
        (snapshot / ".crewplane-generated-file-snapshot.json").read_text(
            encoding="utf-8"
        )
    )

    output.finalize_node(
        node_artifact_request("build.node"),
        generated_file_workspace_roots={
            alpha_output.resolve(strict=False): snapshot,
        },
    )

    result_text = (output.results_dir / build_result_filename("build.node")).read_text(
        encoding="utf-8"
    )
    assert "## Generated Files" not in result_text
    assert snapshot_metadata == {"files": []}
    assert not (
        output.results_dir
        / "generated-files"
        / "build.node"
        / "alpha"
        / "src"
        / "documented.txt"
    ).exists()


def test_workspace_generated_file_snapshot_filters_before_size_limits(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "created.txt").write_text("x", encoding="utf-8")
    (workspace / "src" / "unchanged.txt").write_text(
        "oversized unchanged",
        encoding="utf-8",
    )
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text(
        "\n".join(
            [
                "## Generated Files",
                "",
                "- `src/created.txt`",
                "- `src/unchanged.txt`",
            ]
        ),
        encoding="utf-8",
    )

    with patch(
        "crewplane.artifacts.generated_files.catalog.MAX_GENERATED_FILE_SNAPSHOT_BYTES",
        1,
    ):
        snapshot = snapshot_generated_file_workspace(
            alpha_output,
            workspace,
            changed_paths={"src/created.txt"},
        )
    snapshot_metadata = json.loads(
        (snapshot / ".crewplane-generated-file-snapshot.json").read_text(
            encoding="utf-8"
        )
    )

    assert snapshot_metadata == {
        "files": [{"changed": True, "path": "src/created.txt", "size_bytes": 1}]
    }
    assert (snapshot / "src" / "created.txt").is_file()
    assert not (snapshot / "src" / "unchanged.txt").exists()


def test_workspace_generated_file_snapshot_accepts_missing_output_file_with_candidates(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    created_file = workspace / "src" / "created.txt"
    created_file.write_text("created", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"

    snapshot = snapshot_generated_file_workspace(
        alpha_output,
        workspace,
        candidate_files=(created_file,),
    )
    snapshot_metadata = json.loads(
        (snapshot / ".crewplane-generated-file-snapshot.json").read_text(
            encoding="utf-8"
        )
    )

    assert snapshot_metadata == {
        "files": [
            {"changed": None, "path": "src/created.txt", "size_bytes": len("created")}
        ]
    }
    assert (snapshot / "src" / "created.txt").read_text(encoding="utf-8") == "created"


def test_workspace_generated_file_snapshot_records_files_over_count_limit(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "one.txt").write_text("1", encoding="utf-8")
    (workspace / "src" / "two.txt").write_text("2", encoding="utf-8")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text(
        "\n".join(
            [
                "## Generated Files",
                "",
                "- `src/one.txt`",
                "- `src/two.txt`",
            ]
        ),
        encoding="utf-8",
    )

    with patch(
        "crewplane.artifacts.generated_files.catalog.MAX_GENERATED_FILE_SNAPSHOT_FILES",
        1,
    ):
        snapshot_root = snapshot_generated_file_workspace(alpha_output, workspace)

    metadata = json.loads(
        (snapshot_root / ".crewplane-generated-file-snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(metadata["files"]) == 1
    assert metadata["rejected_file_count"] == 1
    assert not metadata["rejected_files_truncated"]
    assert metadata["rejected_files"] == [
        {
            "configured_limit_count": 1,
            "discovery_source": "provider_explicit_section",
            "disposition": "rejected",
            "explicit": True,
            "path": "src/two.txt",
            "reason": "file_count_limit",
            "size_bytes": 1,
        }
    ]
    assert (snapshot_root / "src" / "one.txt").is_file()
    assert not (snapshot_root / "src" / "two.txt").exists()


def test_workspace_generated_file_snapshot_records_oversized_explicit_file(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    workspace = base_dir / "workspace"
    (workspace / "src").mkdir(parents=True)
    oversized = workspace / "src" / "large.bin"
    oversized.write_bytes(b"xx")
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    alpha_output = stage_dir / "alpha_round1.md"
    alpha_output.write_text(
        "## Generated Files\n\n- `src/large.bin`\n",
        encoding="utf-8",
    )

    with patch(
        "crewplane.artifacts.generated_files.catalog.MAX_GENERATED_FILE_SNAPSHOT_BYTES",
        1,
    ):
        snapshot_root = snapshot_generated_file_workspace(
            alpha_output,
            workspace,
            candidate_files=(oversized,),
            explicit_claims_only=True,
        )

    metadata = json.loads(
        (snapshot_root / ".crewplane-generated-file-snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["files"] == []
    assert metadata["rejected_file_count"] == 1
    assert not metadata["rejected_files_truncated"]
    assert metadata["rejected_files"] == [
        {
            "configured_limit_bytes": 1,
            "discovery_source": "provider_explicit_section",
            "disposition": "rejected",
            "explicit": True,
            "path": "src/large.bin",
            "reason": "per_file_size_limit",
            "size_bytes": 2,
        }
    ]
    assert not (snapshot_root / "src" / "large.bin").exists()
