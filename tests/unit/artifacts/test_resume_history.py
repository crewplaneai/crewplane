from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from crewplane.artifacts.run_history import (
    RunHistoryError,
    find_same_context_runs,
)
from tests.helpers.platforms import requires_posix, symlink_or_skip
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


@pytest.mark.parametrize("manifest_state", ["hardlink", "missing", "permission"])
def test_history_manifest_file_contract(tmp_path, monkeypatch, manifest_state) -> None:
    manifest = make_run_manifest("run", "workflow--run")
    write_run_manifest(tmp_path, manifest)
    manifest_path = (
        tmp_path / "execution-stages" / manifest.run_key_name / "manifests" / "run.json"
    )
    if manifest_state == "hardlink":
        (tmp_path / "manifest-copy").hardlink_to(manifest_path)
    elif manifest_state == "missing":
        manifest_path.unlink()
    else:
        original_lstat = Path.lstat

        def denied_lstat(path):
            if path == manifest_path:
                raise PermissionError("blocked")
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", denied_lstat)

    args = (tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE)
    if manifest_state == "missing":
        assert find_same_context_runs(*args) == ()
    elif manifest_state == "permission":
        with pytest.raises(PermissionError, match="^blocked$"):
            find_same_context_runs(*args)
    else:
        with pytest.raises(
            RunHistoryError, match="^Run history metadata path is not a safe file\\.$"
        ):
            find_same_context_runs(*args)


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
    symlink_or_skip(
        tmp_path / "execution-stages",
        outside_stages,
        target_is_directory=True,
    )

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
    symlink_or_skip(
        symlink_path,
        outside_manifest.parents[1],
        target_is_directory=True,
    )

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
    symlink_or_skip(
        symlink_path,
        outside_manifest.parent,
        target_is_directory=True,
    )

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
    symlink_or_skip(symlink_path, outside_manifest)

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


def test_history_excludes_pre_fix_execution_signature(tmp_path):
    from tests.helpers.execution_semantics import compiled_execution_signatures

    current, previous = compiled_execution_signatures(tmp_path)
    for run, signature in [("previous", previous), ("current", current)]:
        write_run_manifest(
            tmp_path,
            make_run_manifest(
                run,
                f"workflow--{run}",
                status="succeeded",
                workflow_signature=signature,
            ),
        )
    records = find_same_context_runs(
        tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, current
    )
    assert [record.manifest.run_id for record in records] == ["current"]


def test_context_filter_precedes_unrelated_results_containment(tmp_path) -> None:
    unrelated = make_run_manifest("other", "other", workflow_name="Other workflow")
    write_run_manifest(tmp_path, unrelated)
    results = tmp_path / "execution-results"
    results.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(results / "other", outside, target_is_directory=True)
    assert (
        find_same_context_runs(
            tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
        )
        == ()
    )
    with pytest.raises(RunHistoryError, match="escapes"):
        find_same_context_runs(
            tmp_path, WORKFLOW_IDENTITY, "Other workflow", WORKFLOW_SIGNATURE
        )


def test_context_history_preserves_candidate_order_for_equal_instants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = ["z", "a"]
    for key, started in zip(
        keys,
        ["2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+01:00"],
        strict=True,
    ):
        manifest = make_run_manifest(key, key)
        manifest.started_at = started
        write_run_manifest(tmp_path, manifest)
    stages = tmp_path / "execution-stages"
    original_iterdir = Path.iterdir

    def iterdir(path: Path):
        if path == stages:
            return iter(stages / key for key in keys)
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    records = find_same_context_runs(
        tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
    )

    assert [record.manifest.run_id for record in records] == keys
    assert all(not record.results_dir.exists() for record in records)


@pytest.mark.parametrize(
    "started_at", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
)
def test_context_history_retains_offset_instants_outside_utc_datetime_range(
    tmp_path: Path, started_at: str
) -> None:
    manifest = make_run_manifest("extreme", "extreme")
    manifest.started_at = started_at
    write_run_manifest(tmp_path, manifest)

    records = find_same_context_runs(
        tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
    )

    assert [record.manifest for record in records] == [manifest]


def test_context_history_preserves_naive_datetime_range(tmp_path: Path) -> None:
    for key, started_at in [
        ("oldest", "0001-01-01T00:00:00"),
        ("newest", "9999-12-31T23:59:59.999999"),
    ]:
        manifest = make_run_manifest(key, key)
        manifest.started_at = started_at
        write_run_manifest(tmp_path, manifest)

    records = find_same_context_runs(
        tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
    )

    assert [record.manifest.run_id for record in records] == ["newest", "oldest"]


@requires_posix
@pytest.mark.parametrize("timezone", ["UTC0", "UTC+7"])
def test_context_history_compares_naive_local_times_with_offset_instants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timezone: str
) -> None:
    try:
        with monkeypatch.context() as environment:
            environment.setenv("TZ", timezone)
            time.tzset()
            local_start = datetime(2026, 1, 1, 12)
            recorded_instant = local_start.astimezone(UTC)
            for key, started_at in [
                ("local", local_start.isoformat()),
                ("older", (recorded_instant - timedelta(seconds=1)).isoformat()),
                ("newer", (recorded_instant + timedelta(seconds=1)).isoformat()),
            ]:
                manifest = make_run_manifest(key, key)
                manifest.started_at = started_at
                write_run_manifest(tmp_path, manifest)

            records = find_same_context_runs(
                tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
            )

            assert [record.manifest.run_id for record in records] == [
                "newer",
                "local",
                "older",
            ]
            assert records[1].manifest.started_at == local_start.isoformat()
    finally:
        time.tzset()
