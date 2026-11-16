from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from crewplane.artifacts.run_history import RunHistoryError, scan_run_history
from tests.helpers.platforms import symlink_or_skip
from tests.helpers.resume import make_run_manifest, write_run_manifest


def test_absent_history_and_preflight_attempt_are_read_only(tmp_path: Path) -> None:
    state = tmp_path / ".crewplane"
    assert scan_run_history(state).records == ()
    assert not state.exists()
    attempt = state / "execution-stages" / "attempt"
    attempt.mkdir(parents=True)
    before = list(state.rglob("*"))
    result = scan_run_history(state)
    assert result.records == ()
    assert [(item.run_key_name, item.category) for item in result.diagnostics] == [
        ("attempt", "no_manifest")
    ]
    assert list(state.rglob("*")) == before


def test_all_context_order_uses_persisted_instants_and_key_ties(tmp_path: Path) -> None:
    for key, started, name in [
        ("z", "2026-01-01T00:00:00+00:00", "First"),
        ("a", "2026-01-01T01:00:00+01:00", "Second"),
        ("new", "2027-01-01T00:00:00", "Other"),
    ]:
        manifest = make_run_manifest(key, key, workflow_name=name)
        manifest.started_at = started
        write_run_manifest(tmp_path, manifest)
    for index, key in enumerate(("new", "a", "z"), 1):
        os.utime(tmp_path / "execution-stages" / key, (index, index))
    result = scan_run_history(tmp_path)
    assert [item.manifest.run_key_name for item in result.records] == ["new", "a", "z"]
    assert all(not item.results_dir.exists() for item in result.records)


@pytest.mark.parametrize(
    ("key", "changes", "raw", "category"),
    [
        ("broken", {}, b"{", "invalid_json"),
        ("encoding", {}, b"\xff", "invalid_utf8"),
        ("schema", {"run_state_schema_version": -1}, None, "invalid_manifest"),
        ("time", {"started_at": "invalid"}, None, "invalid_manifest"),
        ("unsafe", {"run_key_name": "../outside"}, None, "invalid_key"),
        ("wrong", {"run_key_name": "other"}, None, "key_mismatch"),
    ],
)
def test_excluded_diagnostics_do_not_hide_valid_runs(
    tmp_path: Path,
    key: str,
    changes: dict[str, object],
    raw: bytes | None,
    category: str,
) -> None:
    write_run_manifest(tmp_path, make_run_manifest("valid", "valid"))
    manifest = make_run_manifest(key, key)
    path = write_run_manifest(tmp_path, manifest)
    payload = manifest.model_dump(mode="json") | changes
    path.write_bytes(raw if raw is not None else json.dumps(payload).encode("utf-8"))
    result = scan_run_history(tmp_path)
    assert [record.manifest.run_id for record in result.records] == ["valid"]
    assert result.diagnostics[0].category == category
    assert result.diagnostics[0].path == path
    assert "workflow_source" not in result.diagnostics[0].message


@pytest.mark.parametrize("target", ["state", "stages", "results", "run", "metadata"])
def test_unsafe_components_are_hard_failures(tmp_path: Path, target: str) -> None:
    state = tmp_path / "state"
    path = write_run_manifest(state, make_run_manifest("run", "run"))
    paths = {
        "state": state,
        "stages": state / "execution-stages",
        "results": state / "execution-results",
        "run": path.parents[1],
        "metadata": path.parent,
    }
    selected = paths[target]
    outside = tmp_path / "outside"
    if selected.exists():
        selected.rename(outside)
    else:
        outside.mkdir()
    symlink_or_skip(selected, outside, target_is_directory=True)
    with pytest.raises(RunHistoryError):
        scan_run_history(state)


def test_manifest_replacement_read_failure_is_actionable(tmp_path, monkeypatch) -> None:
    write_run_manifest(tmp_path, make_run_manifest("run", "run"))

    monkeypatch.setattr(
        "crewplane.artifacts.run_history.read_contained_bytes",
        Mock(side_effect=FileNotFoundError("replaced")),
    )
    with pytest.raises(RunHistoryError, match="retry"):
        scan_run_history(tmp_path)


def test_non_directory_root_is_not_empty_history(tmp_path: Path) -> None:
    (tmp_path / "execution-stages").write_bytes(b"not a directory")
    with pytest.raises(RunHistoryError, match="not a directory"):
        scan_run_history(tmp_path)


def test_missing_manifest_does_not_hide_unsafe_metadata_directory(tmp_path) -> None:
    run_dir = tmp_path / "execution-stages" / "run"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(run_dir / "manifests", outside, target_is_directory=True)
    with pytest.raises(RunHistoryError, match="symlink"):
        scan_run_history(tmp_path)


def test_results_alias_within_expected_root_is_rejected(tmp_path) -> None:
    write_run_manifest(tmp_path, make_run_manifest("run", "run"))
    results = tmp_path / "execution-results"
    target = results / "other"
    target.mkdir(parents=True)
    symlink_or_skip(results / "run", target, target_is_directory=True)
    with pytest.raises(RunHistoryError, match="symlink"):
        scan_run_history(tmp_path)


@pytest.mark.race
@pytest.mark.parametrize("corrupt", [False, True])
def test_manifest_parent_substitution_during_protected_read_fails(
    tmp_path, monkeypatch, corrupt
) -> None:
    path = write_run_manifest(tmp_path, make_run_manifest("run", "run"))
    if corrupt:
        path.write_bytes(b"{")
    parent = path.parent
    moved = parent.with_name("moved-manifests")
    real_read = os.read
    attempted = False
    original = path.read_bytes()

    def read_and_substitute(descriptor: int, size: int) -> bytes:
        nonlocal attempted
        payload = real_read(descriptor, size)
        if payload and not attempted:
            attempted = True
            if os.name == "nt":
                with pytest.raises(PermissionError):
                    parent.rename(moved)
            else:
                parent.rename(moved)
                symlink_or_skip(parent, moved, target_is_directory=True)
        return payload

    monkeypatch.setattr(
        "crewplane.architecture.safe_file_reads.os.read", read_and_substitute
    )
    if os.name == "nt":
        history = scan_run_history(tmp_path)
        assert len(history.records) == (0 if corrupt else 1)
        assert [item.category for item in history.diagnostics] == (
            ["invalid_json"] if corrupt else []
        )
        assert path.read_bytes() == original
        assert not moved.exists()
    else:
        with pytest.raises(RunHistoryError, match="symlink|retry"):
            scan_run_history(tmp_path)
    assert attempted


@pytest.mark.parametrize("target", ["root", "run"])
def test_existing_results_paths_must_be_directories(tmp_path, target) -> None:
    write_run_manifest(tmp_path, make_run_manifest("run", "run"))
    path = tmp_path / "execution-results"
    if target == "run":
        path.mkdir()
        path = path / "run"
    path.write_bytes(b"not a directory")
    with pytest.raises(RunHistoryError, match="directory"):
        scan_run_history(tmp_path)


@pytest.mark.parametrize(
    "timestamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
)
def test_unrepresentable_start_instants_are_excluded_with_diagnostics(
    tmp_path, timestamp
) -> None:
    manifest = make_run_manifest("extreme", "extreme")
    manifest.started_at = timestamp
    write_run_manifest(tmp_path, manifest)
    write_run_manifest(tmp_path, make_run_manifest("valid", "valid"))
    history = scan_run_history(tmp_path)
    assert [record.manifest.run_id for record in history.records] == ["valid"]
    assert [(item.run_key_name, item.category) for item in history.diagnostics] == [
        ("extreme", "invalid_manifest")
    ]


@pytest.mark.parametrize(
    "payload",
    [b"9" * 5000, b"[" * 10000 + b"]" * 9999],
    ids=["oversized_integer", "excessive_nesting"],
)
def test_pathological_json_does_not_hide_valid_history(tmp_path, payload) -> None:
    write_run_manifest(tmp_path, make_run_manifest("valid", "valid"))
    corrupt = write_run_manifest(tmp_path, make_run_manifest("corrupt", "corrupt"))
    corrupt.write_bytes(payload)
    history = scan_run_history(tmp_path)
    assert [record.manifest.run_id for record in history.records] == ["valid"]
    assert [(item.run_key_name, item.category) for item in history.diagnostics] == [
        ("corrupt", "invalid_json")
    ]


def test_catalogue_rejects_manifestless_run_alias(tmp_path) -> None:
    stages = tmp_path / "execution-stages"
    target = stages / "real"
    target.mkdir(parents=True)
    symlink_or_skip(stages / "alias", target, target_is_directory=True)
    with pytest.raises(RunHistoryError, match="symlink"):
        scan_run_history(tmp_path)


@pytest.mark.parametrize(
    ("changes", "category"),
    [
        (
            {"started_at": "0001-01-01T00:00:00+01:00", "run_key_name": "../bad"},
            "invalid_manifest",
        ),
        ({"run_key_name": "../bad"}, "invalid_key"),
        ({"run_key_name": "other"}, "key_mismatch"),
    ],
)
def test_manifest_exclusions_precede_results_inspection(
    tmp_path: Path, changes: dict[str, str], category: str
) -> None:
    manifest = make_run_manifest("run", "run")
    path = write_run_manifest(tmp_path, manifest)
    path.write_bytes(json.dumps(manifest.model_dump(mode="json") | changes).encode())
    (tmp_path / "execution-results").write_bytes(b"not a directory")

    history = scan_run_history(tmp_path)

    assert history.records == ()
    assert [(item.run_key_name, item.category) for item in history.diagnostics] == [
        ("run", category)
    ]


def test_diagnostics_follow_directory_order_and_ignore_regular_entries(
    tmp_path: Path,
) -> None:
    stages = tmp_path / "execution-stages"
    (stages / "z-attempt").mkdir(parents=True)
    path = write_run_manifest(tmp_path, make_run_manifest("a", "a-corrupt"))
    path.write_bytes(b"{")
    (stages / "plain-file").write_bytes(b"not a directory")

    history = scan_run_history(tmp_path)

    assert history.records == ()
    assert [(item.run_key_name, item.category) for item in history.diagnostics] == [
        ("a-corrupt", "invalid_json"),
        ("z-attempt", "no_manifest"),
    ]
