from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.cli.workspace_cleanup_evidence_support import (
    RUN_KEY,
    collect_cleanup_evidence,
    snapshot_cache_path,
    snapshot_claim_payload,
    write_cleanup_claim,
    write_cleanup_evidence_plan,
)


def test_cleanup_evidence_distinguishes_no_claim_from_corrupt_run(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    workspace_path = snapshot_cache_path(cache_root)
    empty = collect_cleanup_evidence(tmp_path / "missing", cache_root)

    assert empty.decision(RUN_KEY, workspace_path).deletable is True
    assert empty.status_for_cache_key(RUN_KEY, "snapshot") is None

    state_path = tmp_path / "stages" / RUN_KEY / "node" / "workspace-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not-json", encoding="utf-8")
    corrupt = collect_cleanup_evidence(tmp_path / "stages", cache_root)

    decision = corrupt.decision(RUN_KEY, workspace_path)
    assert decision.deletable is False
    assert decision.status == "invalid"
    assert corrupt.status_for_cache_key(RUN_KEY, "snapshot") == "invalid"


def test_cleanup_evidence_rejects_unsafe_and_malformed_claim_files(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    malformed_runs = {
        "array": [],
        "missing-path": snapshot_claim_payload(workspace_path),
        "relative-path": snapshot_claim_payload(workspace_path),
        "wrong-run": snapshot_claim_payload(workspace_path),
        "missing-git": snapshot_claim_payload(workspace_path),
    }
    malformed_runs["missing-path"]["execution"] = {}
    malformed_runs["relative-path"]["execution"] = {"workspace_path": "relative"}
    malformed_runs["wrong-run"]["run_key_name"] = "different"
    malformed_runs["missing-git"]["git"] = {}
    for run_key, payload in malformed_runs.items():
        write_cleanup_claim(stage_root, run_key, payload)

    target = tmp_path / "target-state.json"
    target.write_text("{}", encoding="utf-8")
    symlink_path = stage_root / "symlink" / "node" / "workspace-state.json"
    symlink_path.parent.mkdir(parents=True)
    symlink_path.symlink_to(target)

    evidence = collect_cleanup_evidence(stage_root, cache_root)

    for run_key in (*malformed_runs, "symlink"):
        assert evidence.decision(run_key, workspace_path).status == "invalid"


def test_cleanup_evidence_rejects_symlinked_claim_directory(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    external = tmp_path / "external"
    write_cleanup_claim(external, RUN_KEY, snapshot_claim_payload(workspace_path))
    run_dir = stage_root / RUN_KEY
    run_dir.mkdir(parents=True)
    (run_dir / "linked").symlink_to(external / RUN_KEY, target_is_directory=True)

    evidence = collect_cleanup_evidence(stage_root, cache_root)

    decision = evidence.decision(RUN_KEY, workspace_path)
    assert decision.status == "invalid"
    assert decision.deletable is False
    assert evidence.status_for_cache_key(RUN_KEY, "snapshot") == "invalid"


def test_cleanup_evidence_ignores_state_names_outside_node_stage_roots(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    forged_path = (
        stage_root / RUN_KEY / "manifests" / "nodes" / "workspace-state-forged.json"
    )
    forged_path.parent.mkdir(parents=True)
    forged_path.write_text(
        json.dumps(snapshot_claim_payload(workspace_path)),
        encoding="utf-8",
    )
    write_cleanup_evidence_plan(stage_root, RUN_KEY)

    evidence = collect_cleanup_evidence(stage_root, cache_root)

    assert evidence.status_for_cache_key(RUN_KEY, workspace_path.name) is None
    assert evidence.decision(RUN_KEY, workspace_path).status is None


def test_cleanup_evidence_uses_planned_nested_node_stage_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    run_dir = stage_root / RUN_KEY
    write_cleanup_evidence_plan(stage_root, RUN_KEY, stage_path="custom/build-stage")
    state_path = run_dir / "custom" / "build-stage" / "workspace-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(snapshot_claim_payload(workspace_path)),
        encoding="utf-8",
    )

    decision = collect_cleanup_evidence(stage_root, cache_root).decision(
        RUN_KEY, workspace_path
    )

    assert decision.status == "succeeded"
    assert decision.state_paths == (state_path,)


def test_cleanup_evidence_rejects_plan_that_omits_an_ordered_node(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    write_cleanup_claim(stage_root, RUN_KEY, snapshot_claim_payload(workspace_path))
    plan_path = stage_root / RUN_KEY / "preflight" / "execution-plan.json"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["nodes"] = []
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    decision = collect_cleanup_evidence(stage_root, cache_root).decision(
        RUN_KEY, workspace_path
    )

    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_rejects_symlinked_stage_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    workspace_path = snapshot_cache_path(cache_root)
    external = tmp_path / "external"
    external.mkdir()
    stage_root = tmp_path / "stages"
    stage_root.symlink_to(external, target_is_directory=True)

    evidence = collect_cleanup_evidence(stage_root, cache_root)

    decision = evidence.decision(RUN_KEY, workspace_path)
    assert decision.status == "invalid"
    assert decision.deletable is False
    assert evidence.status_for_cache_key(RUN_KEY, "snapshot") == "invalid"


def test_cleanup_evidence_rejects_non_directory_stage_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    workspace_path = snapshot_cache_path(cache_root)
    stage_root = tmp_path / "stages"
    stage_root.write_text("not a directory", encoding="utf-8")

    evidence = collect_cleanup_evidence(stage_root, cache_root)

    decision = evidence.decision(RUN_KEY, workspace_path)
    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_rejects_symlinked_run_directory(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    workspace_path = snapshot_cache_path(cache_root)
    stage_root = tmp_path / "stages"
    stage_root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (stage_root / RUN_KEY).symlink_to(external, target_is_directory=True)
    (stage_root / "unrelated-file").write_text("ignored", encoding="utf-8")

    evidence = collect_cleanup_evidence(stage_root, cache_root)

    decision = evidence.decision(RUN_KEY, workspace_path)
    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_accepts_one_valid_snapshot_claim(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    state_path = write_cleanup_claim(
        stage_root,
        RUN_KEY,
        snapshot_claim_payload(workspace_path),
    )

    evidence = collect_cleanup_evidence(stage_root, cache_root)
    decision = evidence.decision(RUN_KEY, workspace_path)

    assert decision.deletable is True
    assert decision.status == "succeeded"
    assert decision.state_paths == (state_path,)
    assert evidence.status_for_cache_key(RUN_KEY, "snapshot") == "succeeded"


@pytest.mark.parametrize("directory_name", ["logs", "node"])
def test_cleanup_evidence_rejects_unreadable_evidence_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_name: str
) -> None:
    cache = tmp_path / "cache"
    stages = tmp_path / "stages"
    workspace = snapshot_cache_path(cache)
    (workspace / "checkout").mkdir(parents=True)
    write_cleanup_claim(stages, RUN_KEY, snapshot_claim_payload(workspace))
    blocked = stages / RUN_KEY / directory_name
    blocked.mkdir(exist_ok=True)
    original = Path.iterdir

    def read_directory(path: Path) -> Iterator[Path]:
        if path == blocked:
            raise PermissionError("directory unavailable")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "iterdir", read_directory)
        evidence = collect_cleanup_evidence(stages, cache)

    assert not evidence.decision(RUN_KEY, workspace).deletable
    assert evidence.ref_cleanup_run_keys() == ()
    assert evidence.absent_state_projections() == ()


@pytest.mark.parametrize("target", ["claim", "plan", "manifest"])
def test_cleanup_evidence_rejects_file_that_becomes_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    cache = tmp_path / "cache"
    stages = tmp_path / "stages"
    workspace = snapshot_cache_path(cache)
    (workspace / "checkout").mkdir(parents=True)
    claim = write_cleanup_claim(stages, RUN_KEY, snapshot_claim_payload(workspace))
    paths = {
        "claim": claim,
        "plan": stages / RUN_KEY / "preflight" / "execution-plan.json",
        "manifest": stages / RUN_KEY / "manifests" / "run.json",
    }
    original = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == paths[target]:
            raise PermissionError("file unavailable")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read_text)
        decision = collect_cleanup_evidence(stages, cache).decision(RUN_KEY, workspace)

    assert not decision.deletable
    assert decision.status == "invalid"


@pytest.mark.parametrize("shape", ["file", "symlink", "hardlinked-evidence"])
def test_cleanup_evidence_rejects_unsafe_ref_evidence(
    tmp_path: Path, shape: str
) -> None:
    cache = tmp_path / "cache"
    stages = tmp_path / "stages"
    workspace = snapshot_cache_path(cache)
    (workspace / "checkout").mkdir(parents=True)
    write_cleanup_claim(stages, RUN_KEY, snapshot_claim_payload(workspace))
    logs = stages / RUN_KEY / "logs"
    if shape == "file":
        logs.write_text("not a directory", encoding="utf-8")
    elif shape == "symlink":
        logs.symlink_to(tmp_path, target_is_directory=True)
    else:
        logs.mkdir()
        evidence_file = logs / "workspace-temporary-refs-cleanup.json"
        evidence_file.write_text("{}", encoding="utf-8")
        (tmp_path / "alias.json").hardlink_to(evidence_file)

    evidence = collect_cleanup_evidence(stages, cache)
    assert not evidence.decision(RUN_KEY, workspace).deletable
    assert evidence.ref_cleanup_run_keys() == ()


def test_cleanup_evidence_rejects_claim_disappearing_during_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    stages = tmp_path / "stages"
    workspace = snapshot_cache_path(cache)
    (workspace / "checkout").mkdir(parents=True)
    claim = write_cleanup_claim(stages, RUN_KEY, snapshot_claim_payload(workspace))
    original = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        if path == claim:
            raise FileNotFoundError("claim removed concurrently")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", lstat)
        decision = collect_cleanup_evidence(stages, cache).decision(RUN_KEY, workspace)

    assert not decision.deletable


@pytest.mark.parametrize("relative_root", [None, "/outside", "../outside"])
def test_cleanup_evidence_rejects_unverifiable_project_placement(
    tmp_path: Path, relative_root: str | None
) -> None:
    cache = tmp_path / "cache"
    stages = tmp_path / "stages"
    workspace = snapshot_cache_path(cache)
    (workspace / "checkout").mkdir(parents=True)
    payload = snapshot_claim_payload(workspace)
    payload["workspace"]["project_root_relative_path"] = relative_root
    write_cleanup_claim(stages, RUN_KEY, payload)

    decision = collect_cleanup_evidence(stages, cache).decision(RUN_KEY, workspace)

    assert not decision.deletable
    assert decision.status == "invalid"
