from __future__ import annotations

import os
from pathlib import Path

import pytest

from crewplane.artifacts.naming import build_node_state_filename
from crewplane.artifacts.resume.validation import (
    contained_regular_file,
    validate_resume_frontier,
)
from tests.helpers.resume import (
    make_node_state,
    make_plan,
    write_node_state,
    write_result,
)
from tests.helpers.resume_validation import source_record


def test_symlink_result_is_not_reusable(tmp_path) -> None:
    source = source_record(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("a output", encoding="utf-8")
    result_path = source.results_dir / "a-result.md"
    result_path.parent.mkdir(parents=True)
    os.symlink(outside, result_path)
    descriptor = write_result(tmp_path / "descriptor-source", "a-result.md", "a output")
    descriptor = descriptor.model_copy(update={"relative_path": "a-result.md"})
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_hardlinked_result_is_not_reusable(tmp_path) -> None:
    source = source_record(tmp_path)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    os.link(source.results_dir / "a-result.md", source.results_dir / "a-copy.md")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_symlink_results_root_is_not_reusable(tmp_path) -> None:
    source = source_record(tmp_path)
    outside_results = tmp_path / "outside-results"
    descriptor = write_result(outside_results, "a-result.md", "a output")
    source.results_dir.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside_results, source.results_dir, target_is_directory=True)
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_symlink_results_root_parent_is_not_reusable(tmp_path) -> None:
    real_root = tmp_path / "real-results"
    root = tmp_path / "execution-results" / "run"
    result = real_root / "run" / "a-result.md"
    result.parent.mkdir(parents=True)
    result.write_text("a output", encoding="utf-8")
    os.symlink(real_root, root.parent, target_is_directory=True)

    assert contained_regular_file(root, "a-result.md") is None


def test_symlink_node_state_file_is_not_reusable(tmp_path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink support is unavailable")
    source = source_record(tmp_path)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    external_state_path = write_node_state(
        tmp_path / "outside-state",
        make_node_state(source.manifest, "a", [descriptor]),
    )
    node_state_dir = source.run_dir / "manifests" / "nodes"
    node_state_dir.mkdir(parents=True)
    symlink_path = node_state_dir / build_node_state_filename("a")
    try:
        symlink_path.symlink_to(external_state_path)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_symlink_node_state_directory_is_not_reusable(tmp_path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink support is unavailable")
    source = source_record(tmp_path)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    external_state_path = write_node_state(
        tmp_path / "outside-state",
        make_node_state(source.manifest, "a", [descriptor]),
    )
    node_state_parent = source.run_dir / "manifests"
    node_state_parent.mkdir(exist_ok=True)
    symlink_path = node_state_parent / "nodes"
    try:
        symlink_path.symlink_to(
            external_state_path.parent,
            target_is_directory=True,
        )
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_hardlinked_node_state_file_is_not_reusable(tmp_path) -> None:
    source = source_record(tmp_path)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    node_state_path = write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    try:
        os.link(node_state_path, node_state_path.with_suffix(".copy.json"))
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    frontier = validate_resume_frontier(source, make_plan())

    assert frontier.resumed_node_ids == ()


def test_permission_error_during_artifact_validation_fails_loudly(
    tmp_path,
    monkeypatch,
) -> None:
    source = source_record(tmp_path)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    original_resolve = Path.resolve

    def resolve_with_permission_error(self, strict=False):
        if self.name == "a-result.md":
            raise PermissionError("blocked")
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_with_permission_error)

    with pytest.raises(PermissionError, match="blocked"):
        validate_resume_frontier(source, make_plan())


def test_contained_regular_file_rejects_empty_path_segments(tmp_path) -> None:
    result = tmp_path / "a-result.md"
    result.write_text("a output", encoding="utf-8")

    assert contained_regular_file(tmp_path, "nested//a-result.md") is None
