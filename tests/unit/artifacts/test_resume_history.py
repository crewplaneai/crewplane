from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from crewplane.artifacts.run_history import (
    RunHistoryError,
    find_same_context_runs,
)
from tests.helpers.resume import (
    RUNTIME_SIGNATURE,
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    make_run_manifest,
    write_run_manifest,
)


def test_history_scans_current_run_manifests_sorted_by_started_at(tmp_path) -> None:
    older = make_run_manifest(
        "older",
        "workflow--older",
        status="failed",
        started_offset=0,
    )
    newer = make_run_manifest(
        "newer",
        "workflow--newer",
        status="cancelled",
        started_offset=10,
    )
    write_run_manifest(tmp_path, older)
    write_run_manifest(tmp_path, newer)

    records = find_same_context_runs(
        tmp_path,
        WORKFLOW_IDENTITY,
        WORKFLOW_NAME,
        WORKFLOW_SIGNATURE,
    )

    assert [record.manifest.run_id for record in records] == ["newer", "older"]


def test_history_ignores_corrupt_malformed_and_wrong_context_records(tmp_path) -> None:
    valid = make_run_manifest("valid", "workflow--valid", status="succeeded")
    write_run_manifest(tmp_path, valid)
    corrupt_path = tmp_path / "execution-stages" / "corrupt" / "manifests" / "run.json"
    corrupt_path.parent.mkdir(parents=True)
    corrupt_path.write_text("{", encoding="utf-8")
    bad_timestamp = make_run_manifest("bad-time", "workflow--bad-time").model_dump(
        mode="json",
        exclude_none=True,
    )
    bad_timestamp["started_at"] = "not-a-date"
    bad_path = tmp_path / "execution-stages" / "bad" / "manifests" / "run.json"
    bad_path.parent.mkdir(parents=True)
    bad_path.write_text(json.dumps(bad_timestamp), encoding="utf-8")
    missing_schema = make_run_manifest(
        "missing-schema",
        "workflow--missing-schema",
    ).model_dump(mode="json", exclude_none=True)
    del missing_schema["run_state_schema_version"]
    missing_schema_path = (
        tmp_path
        / "execution-stages"
        / "workflow--missing-schema"
        / "manifests"
        / "run.json"
    )
    missing_schema_path.parent.mkdir(parents=True)
    missing_schema_path.write_text(json.dumps(missing_schema), encoding="utf-8")
    wrong_context = make_run_manifest(
        "wrong",
        "workflow--wrong",
        workflow_signature=RUNTIME_SIGNATURE,
    )
    write_run_manifest(tmp_path, wrong_context)

    records = find_same_context_runs(
        tmp_path,
        WORKFLOW_IDENTITY,
        WORKFLOW_NAME,
        WORKFLOW_SIGNATURE,
    )

    assert [record.manifest.run_id for record in records] == ["valid"]


def test_history_stages_root_permission_error_fails_loudly(
    tmp_path,
    monkeypatch,
) -> None:
    stages_root = tmp_path / "execution-stages"
    original_lstat = Path.lstat

    def lstat_with_permission_error(self):
        if self == stages_root:
            raise PermissionError("blocked")
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", lstat_with_permission_error)

    with pytest.raises(PermissionError, match="blocked"):
        find_same_context_runs(
            tmp_path,
            WORKFLOW_IDENTITY,
            WORKFLOW_NAME,
            WORKFLOW_SIGNATURE,
        )


def test_history_rejects_symlinked_stages_root(tmp_path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink support is unavailable")
    outside_stages = tmp_path / "outside-stages"
    outside_stages.mkdir()
    try:
        (tmp_path / "execution-stages").symlink_to(
            outside_stages,
            target_is_directory=True,
        )
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(RunHistoryError, match="history root contains a symlink"):
        find_same_context_runs(
            tmp_path,
            WORKFLOW_IDENTITY,
            WORKFLOW_NAME,
            WORKFLOW_SIGNATURE,
        )


def test_history_rejects_symlinked_run_directory_escape(tmp_path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink support is unavailable")
    stages_root = tmp_path / "execution-stages"
    stages_root.mkdir()
    manifest = make_run_manifest(
        "external",
        "linked-run",
        status="succeeded",
    )
    write_run_manifest(tmp_path / "outside-container", manifest)
    outside_manifest = (
        tmp_path
        / "outside-container"
        / "execution-stages"
        / manifest.run_key_name
        / "manifests"
        / "run.json"
    )
    symlink_path = stages_root / manifest.run_key_name
    try:
        symlink_path.symlink_to(
            outside_manifest.parents[1],
            target_is_directory=True,
        )
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(RunHistoryError, match="escapes"):
        find_same_context_runs(
            tmp_path,
            WORKFLOW_IDENTITY,
            WORKFLOW_NAME,
            WORKFLOW_SIGNATURE,
        )


def test_history_rejects_symlinked_manifest_directory_escape(tmp_path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink support is unavailable")
    stages_root = tmp_path / "execution-stages"
    stages_root.mkdir()
    manifest = make_run_manifest(
        "external",
        "linked-manifest-dir",
        status="succeeded",
    )
    write_run_manifest(tmp_path / "outside-container", manifest)
    outside_manifest = (
        tmp_path
        / "outside-container"
        / "execution-stages"
        / manifest.run_key_name
        / "manifests"
        / "run.json"
    )
    run_dir = stages_root / manifest.run_key_name
    run_dir.mkdir()
    symlink_path = run_dir / "manifests"
    try:
        symlink_path.symlink_to(
            outside_manifest.parent,
            target_is_directory=True,
        )
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(RunHistoryError, match="symlink"):
        find_same_context_runs(
            tmp_path,
            WORKFLOW_IDENTITY,
            WORKFLOW_NAME,
            WORKFLOW_SIGNATURE,
        )


def test_history_rejects_symlinked_run_manifest_escape(tmp_path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink support is unavailable")
    stages_root = tmp_path / "execution-stages"
    stages_root.mkdir()
    manifest = make_run_manifest(
        "external",
        "linked-run-manifest",
        status="succeeded",
    )
    write_run_manifest(tmp_path / "outside-container", manifest)
    outside_manifest = (
        tmp_path
        / "outside-container"
        / "execution-stages"
        / manifest.run_key_name
        / "manifests"
        / "run.json"
    )
    manifest_dir = stages_root / manifest.run_key_name / "manifests"
    manifest_dir.mkdir(parents=True)
    symlink_path = manifest_dir / "run.json"
    try:
        symlink_path.symlink_to(outside_manifest)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(RunHistoryError, match="symlink"):
        find_same_context_runs(
            tmp_path,
            WORKFLOW_IDENTITY,
            WORKFLOW_NAME,
            WORKFLOW_SIGNATURE,
        )


def test_history_ignores_records_with_unsafe_or_mismatched_run_keys(tmp_path) -> None:
    valid = make_run_manifest("valid", "workflow--valid", status="succeeded")
    write_run_manifest(tmp_path, valid)
    unsafe = make_run_manifest("unsafe", "workflow--unsafe").model_dump(
        mode="json",
        exclude_none=True,
    )
    unsafe["run_key_name"] = "../../outside"
    unsafe_path = tmp_path / "execution-stages" / "unsafe" / "manifests" / "run.json"
    unsafe_path.parent.mkdir(parents=True)
    unsafe_path.write_text(json.dumps(unsafe), encoding="utf-8")
    mismatched = make_run_manifest(
        "mismatched",
        "workflow--other",
        status="failed",
    ).model_dump(mode="json", exclude_none=True)
    mismatched_path = (
        tmp_path / "execution-stages" / "mismatched" / "manifests" / "run.json"
    )
    mismatched_path.parent.mkdir(parents=True)
    mismatched_path.write_text(json.dumps(mismatched), encoding="utf-8")

    records = find_same_context_runs(
        tmp_path,
        WORKFLOW_IDENTITY,
        WORKFLOW_NAME,
        WORKFLOW_SIGNATURE,
    )

    assert [record.manifest.run_id for record in records] == ["valid"]
