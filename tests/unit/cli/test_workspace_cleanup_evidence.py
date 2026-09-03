from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from crewplane.cli.workspace_cleanup_evidence import WorkspaceCleanupEvidence
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_run_manifest
from tests.helpers.workspace_records import (
    WORKTREE_CONTRACT_PAYLOAD,
    workspace_selection_record,
)
from tests.helpers.workspace_service import disabled_workspace_plan

REPOSITORY_ID = "repo"
RUN_KEY = "run-key"
OID_A = "a" * 40
OID_B = "b" * 40


def test_cleanup_evidence_distinguishes_no_claim_from_corrupt_run(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    workspace_path = _snapshot_path(cache_root)
    empty = _evidence(tmp_path / "missing", cache_root)

    assert empty.decision(RUN_KEY, workspace_path).deletable is True
    assert empty.status_for_cache_key(RUN_KEY, "snapshot") is None

    state_path = tmp_path / "stages" / RUN_KEY / "node" / "workspace-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not-json", encoding="utf-8")
    corrupt = _evidence(tmp_path / "stages", cache_root)

    decision = corrupt.decision(RUN_KEY, workspace_path)
    assert decision.deletable is False
    assert decision.status == "invalid"
    assert corrupt.status_for_cache_key(RUN_KEY, "snapshot") == "invalid"


def test_cleanup_evidence_rejects_unsafe_and_malformed_claim_files(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    malformed_runs = {
        "array": [],
        "missing-path": _snapshot_payload(workspace_path),
        "relative-path": _snapshot_payload(workspace_path),
        "wrong-run": _snapshot_payload(workspace_path),
        "missing-git": _snapshot_payload(workspace_path),
    }
    malformed_runs["missing-path"]["execution"] = {}
    malformed_runs["relative-path"]["execution"] = {"workspace_path": "relative"}
    malformed_runs["wrong-run"]["run_key_name"] = "different"
    malformed_runs["missing-git"]["git"] = {}
    for run_key, payload in malformed_runs.items():
        _write_claim(stage_root, run_key, payload)

    target = tmp_path / "target-state.json"
    target.write_text("{}", encoding="utf-8")
    symlink_path = stage_root / "symlink" / "node" / "workspace-state.json"
    symlink_path.parent.mkdir(parents=True)
    symlink_path.symlink_to(target)

    evidence = _evidence(stage_root, cache_root)

    for run_key in (*malformed_runs, "symlink"):
        assert evidence.decision(run_key, workspace_path).status == "invalid"


def test_cleanup_evidence_rejects_symlinked_claim_directory(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    external = tmp_path / "external"
    _write_claim(external, RUN_KEY, _snapshot_payload(workspace_path))
    run_dir = stage_root / RUN_KEY
    run_dir.mkdir(parents=True)
    (run_dir / "linked").symlink_to(external / RUN_KEY, target_is_directory=True)

    evidence = _evidence(stage_root, cache_root)

    decision = evidence.decision(RUN_KEY, workspace_path)
    assert decision.status == "invalid"
    assert decision.deletable is False
    assert evidence.status_for_cache_key(RUN_KEY, "snapshot") == "invalid"


def test_cleanup_evidence_ignores_state_names_outside_node_stage_roots(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    forged_path = (
        stage_root / RUN_KEY / "manifests" / "nodes" / "workspace-state-forged.json"
    )
    forged_path.parent.mkdir(parents=True)
    forged_path.write_text(
        json.dumps(_snapshot_payload(workspace_path)),
        encoding="utf-8",
    )
    _write_plan(stage_root, RUN_KEY)

    evidence = _evidence(stage_root, cache_root)

    assert evidence.status_for_cache_key(RUN_KEY, workspace_path.name) is None
    assert evidence.decision(RUN_KEY, workspace_path).status is None


def test_cleanup_evidence_uses_planned_nested_node_stage_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    run_dir = stage_root / RUN_KEY
    _write_plan(stage_root, RUN_KEY, stage_path="custom/build-stage")
    state_path = run_dir / "custom" / "build-stage" / "workspace-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(_snapshot_payload(workspace_path)),
        encoding="utf-8",
    )

    decision = _evidence(stage_root, cache_root).decision(RUN_KEY, workspace_path)

    assert decision.status == "succeeded"
    assert decision.state_paths == (state_path,)


def test_cleanup_evidence_rejects_plan_that_omits_an_ordered_node(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    _write_claim(stage_root, RUN_KEY, _snapshot_payload(workspace_path))
    plan_path = stage_root / RUN_KEY / "preflight" / "execution-plan.json"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["nodes"] = []
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    decision = _evidence(stage_root, cache_root).decision(RUN_KEY, workspace_path)

    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_rejects_symlinked_stage_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    workspace_path = _snapshot_path(cache_root)
    external = tmp_path / "external"
    external.mkdir()
    stage_root = tmp_path / "stages"
    stage_root.symlink_to(external, target_is_directory=True)

    evidence = _evidence(stage_root, cache_root)

    decision = evidence.decision(RUN_KEY, workspace_path)
    assert decision.status == "invalid"
    assert decision.deletable is False
    assert evidence.status_for_cache_key(RUN_KEY, "snapshot") == "invalid"


def test_cleanup_evidence_rejects_non_directory_stage_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    workspace_path = _snapshot_path(cache_root)
    stage_root = tmp_path / "stages"
    stage_root.write_text("not a directory", encoding="utf-8")

    evidence = _evidence(stage_root, cache_root)

    decision = evidence.decision(RUN_KEY, workspace_path)
    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_rejects_symlinked_run_directory(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    workspace_path = _snapshot_path(cache_root)
    stage_root = tmp_path / "stages"
    stage_root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (stage_root / RUN_KEY).symlink_to(external, target_is_directory=True)
    (stage_root / "unrelated-file").write_text("ignored", encoding="utf-8")

    evidence = _evidence(stage_root, cache_root)

    decision = evidence.decision(RUN_KEY, workspace_path)
    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_accepts_one_valid_snapshot_claim(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    state_path = _write_claim(
        stage_root,
        RUN_KEY,
        _snapshot_payload(workspace_path),
    )

    evidence = _evidence(stage_root, cache_root)
    decision = evidence.decision(RUN_KEY, workspace_path)

    assert decision.deletable is True
    assert decision.status == "succeeded"
    assert decision.state_paths == (state_path,)
    assert evidence.status_for_cache_key(RUN_KEY, "snapshot") == "succeeded"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "different-run-id"),
        ("run_key_name", "different-run-key"),
        ("workflow_name", "different-workflow"),
        ("workflow_signature", "f" * 64),
        ("node_id", "different-node"),
    ],
)
def test_cleanup_evidence_rejects_claim_identity_that_conflicts_with_plan(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    payload = _snapshot_payload(workspace_path)
    payload[field] = value
    _write_claim(stage_root, RUN_KEY, payload)

    evidence = _evidence(stage_root, cache_root)
    decision = evidence.decision(RUN_KEY, workspace_path)

    assert decision.status == "invalid"
    assert decision.deletable is False
    assert evidence.ref_cleanup_run_keys() == ()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "logical_worktree_name", "other"),
        (None, "clean_start", "tracked_only"),
        (None, "worktree_contract", {"mode": "other", "schema_version": "1"}),
        ("workspace", "materialization", "worktree_checkout"),
        ("workspace", "writable", False),
        ("workspace", "lineage_producer", True),
    ],
)
def test_cleanup_evidence_rejects_claim_that_conflicts_with_workspace_policy(
    tmp_path: Path,
    section: str | None,
    field: str,
    value: object,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    payload = _snapshot_payload(workspace_path)
    target = payload if section is None else payload[section]
    assert isinstance(target, dict)
    target[field] = value
    _write_claim(stage_root, RUN_KEY, payload)

    decision = _evidence(stage_root, cache_root).decision(RUN_KEY, workspace_path)

    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_rejects_claim_without_planned_workspace_policy(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    _write_claim(stage_root, RUN_KEY, _snapshot_payload(workspace_path))
    plan_path = stage_root / RUN_KEY / "preflight" / "execution-plan.json"
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_payload["nodes"][0]["workspace_policy"] = None
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

    decision = _evidence(stage_root, cache_root).decision(RUN_KEY, workspace_path)

    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_checks_hydrated_claim_identity_before_ignoring_path(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    payload = _snapshot_payload(workspace_path)
    workspace = payload["workspace"]
    execution = payload["execution"]
    assert isinstance(workspace, dict)
    assert isinstance(execution, dict)
    payload["resume_origin"] = {
        "source_run_id": "source-run",
        "source_run_key_name": "source-run-key",
        "source_node_id": "build",
        "hydrated_at": "2026-08-30T00:00:00+00:00",
        "source_workspace": dict(workspace),
        "source_execution": dict(execution),
    }
    for field in ("path", "effective_cwd", "cache_root", "checkout_root", "cache_key"):
        workspace[field] = None
    workspace["retention"] = "not_applicable"
    workspace["retained_reason"] = "hydrated_resume"
    for field in (
        "cache_root",
        "workspace_path",
        "checkout_root",
        "effective_cwd",
        "worktree_git_dir",
    ):
        execution[field] = None
    payload["workflow_signature"] = "f" * 64
    _write_claim(stage_root, RUN_KEY, payload)

    decision = _evidence(stage_root, cache_root).decision(RUN_KEY, workspace_path)

    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_rejects_reappeared_deleted_workspace(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(parents=True)
    (checkout_root / "replacement.txt").write_text("replacement", encoding="utf-8")
    payload = _snapshot_payload(workspace_path)
    workspace = payload["workspace"]
    assert isinstance(workspace, dict)
    workspace["retention"] = "deleted"
    _write_claim(stage_root, RUN_KEY, payload)

    decision = _evidence(stage_root, cache_root).decision(RUN_KEY, workspace_path)

    assert decision.status == "invalid"
    assert decision.deletable is False
    assert "reappeared after deletion" in str(decision.reason)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("workspace", "path", "/different/workspace"),
        ("workspace", "cache_key", "different-cache-key"),
        ("workspace", "effective_cwd", "/different/checkout"),
        ("execution", "cache_root", "/different/cache"),
        ("execution", "checkout_root", "/different/checkout"),
        ("execution", "effective_cwd", "/different/checkout"),
    ],
)
def test_cleanup_evidence_rejects_contradictory_placement_fields(
    tmp_path: Path,
    section: str,
    field: str,
    value: str,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    payload = _snapshot_payload(workspace_path)
    placement = payload[section]
    assert isinstance(placement, dict)
    placement[field] = value
    _write_claim(stage_root, RUN_KEY, payload)

    decision = _evidence(stage_root, cache_root).decision(RUN_KEY, workspace_path)

    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_rejects_workspace_kind_cache_family_mismatch(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = cache_root / "workspaces" / REPOSITORY_ID / RUN_KEY / "snapshot"
    (workspace_path / "checkout").mkdir(parents=True)
    _write_claim(stage_root, RUN_KEY, _snapshot_payload(workspace_path))

    decision = _evidence(stage_root, cache_root).decision(RUN_KEY, workspace_path)

    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_projects_coherent_terminal_claim_when_path_is_absent(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _snapshot_path(cache_root)
    state_path = _write_claim(
        stage_root,
        RUN_KEY,
        _snapshot_payload(workspace_path),
    )

    assert _evidence(stage_root, cache_root).absent_state_projections() == (
        (RUN_KEY, workspace_path, "succeeded", (state_path,)),
    )


def test_cleanup_evidence_reports_identity_status_and_contract_blockers(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    cases = {
        "outside": (
            tmp_path / "outside",
            _snapshot_payload(tmp_path / "outside", cache_root=cache_root),
            "cache family",
        ),
        "wrong-repository": (
            _snapshot_path(cache_root, "wrong-repository"),
            _snapshot_payload(_snapshot_path(cache_root, "wrong-repository")),
            "contradict repository",
        ),
        "planned": (
            _snapshot_path(cache_root, "planned"),
            _snapshot_payload(_snapshot_path(cache_root, "planned")),
            "workspace state is planned",
        ),
        "bad-contract": (
            _snapshot_path(cache_root, "bad-contract"),
            _snapshot_payload(_snapshot_path(cache_root, "bad-contract")),
            "hardening evidence",
        ),
    }
    wrong_repo = cases["wrong-repository"][1]
    assert isinstance(wrong_repo["git"], dict)
    wrong_repo["git"]["repo_id"] = "other"
    cases["planned"][1]["status"] = "planned"
    cases["bad-contract"][1]["role"] = "observer"
    for run_key, case in cases.items():
        payload = case[1]
        payload["run_id"] = f"{run_key}-id"
        payload["run_key_name"] = run_key
        _write_claim(stage_root, run_key, payload)

    evidence = _evidence(stage_root, cache_root)

    for run_key, case in cases.items():
        workspace_path, _, reason = case
        decision = evidence.decision(run_key, workspace_path)
        assert decision.deletable is False
        assert reason in str(decision.reason)


def test_cleanup_evidence_rejects_conflicting_terminal_and_kind_claims(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"

    terminal_path = _snapshot_path(cache_root, "terminal-conflict", "terminal")
    first = _snapshot_payload(terminal_path, run_key="terminal-conflict")
    first["status"] = "planned"
    second = deepcopy(first)
    second["status"] = "materialized"
    _write_claim(stage_root, "terminal-conflict", first, "workspace-state-1.json")
    _write_claim(stage_root, "terminal-conflict", second, "workspace-state-2.json")

    kind_path = _snapshot_path(cache_root, "kind-conflict", "kind")
    snapshot = _snapshot_payload(kind_path, run_key="kind-conflict")
    worktree = _worktree_payload(kind_path, run_key="kind-conflict", generation=1)
    _write_claim(stage_root, "kind-conflict", snapshot, "workspace-state-1.json")
    _write_claim(stage_root, "kind-conflict", worktree, "workspace-state-2.json")

    evidence = _evidence(stage_root, cache_root)

    assert "terminal outcome" in str(
        evidence.decision("terminal-conflict", terminal_path).reason
    )
    assert "malformed" in str(evidence.decision("kind-conflict", kind_path).reason)


def test_cleanup_evidence_rejects_invalid_snapshot_physical_evidence(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"

    duplicate_path = _snapshot_path(cache_root, "duplicate", "snapshot")
    duplicate_path.joinpath("checkout").mkdir(parents=True)
    duplicate = _snapshot_payload(duplicate_path, run_key="duplicate")
    _write_claim(stage_root, "duplicate", duplicate, "workspace-state-1.json")
    _write_claim(stage_root, "duplicate", duplicate, "workspace-state-2.json")

    missing_path = _snapshot_path(cache_root, "missing", "snapshot")
    _write_claim(
        stage_root,
        "missing",
        _snapshot_payload(missing_path, run_key="missing"),
    )

    git_path = _snapshot_path(cache_root, "git-admin", "snapshot")
    (git_path / "checkout" / ".git").mkdir(parents=True)
    _write_claim(
        stage_root,
        "git-admin",
        _snapshot_payload(git_path, run_key="git-admin"),
    )

    evidence = _evidence(stage_root, cache_root)

    assert "unexpected generation claims" in str(
        evidence.decision("duplicate", duplicate_path).reason
    )
    assert "not a real checkout" in str(
        evidence.decision("missing", missing_path).reason
    )
    assert "Git administration" in str(evidence.decision("git-admin", git_path).reason)


def test_cleanup_evidence_rejects_invalid_worktree_generations_and_identity(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    cases = {
        "missing-generation": (None,),
        "duplicate-generation": (1, 1),
        "nonmonotonic-generation": (1, 3),
        "unregistered": (1,),
    }
    paths: dict[str, Path] = {}
    for run_key, generations in cases.items():
        workspace_path = _worktree_path(cache_root, run_key, "workspace")
        paths[run_key] = workspace_path
        if run_key == "unregistered":
            (workspace_path / "checkout").mkdir(parents=True)
        for index, generation in enumerate(generations, start=1):
            payload = _worktree_payload(
                workspace_path,
                run_key=run_key,
                generation=generation,
            )
            _write_claim(
                stage_root,
                run_key,
                payload,
                f"workspace-state-{index}.json",
            )

    evidence = _evidence(stage_root, cache_root)

    assert "positive reuse generation" in str(
        evidence.decision("missing-generation", paths["missing-generation"]).reason
    )
    assert "duplicate generation" in str(
        evidence.decision("duplicate-generation", paths["duplicate-generation"]).reason
    )
    assert "coherent and monotonic" in str(
        evidence.decision(
            "nonmonotonic-generation", paths["nonmonotonic-generation"]
        ).reason
    )
    assert "identity or disposal safety" in str(
        evidence.decision("unregistered", paths["unregistered"]).reason
    )


def test_logical_blocker_precedes_variant_and_physical_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _worktree_path(cache_root, RUN_KEY, "workspace")
    payload = _worktree_payload(workspace_path, generation=None)
    payload["workspace_mutator"] = {"status": "unresolved"}
    _write_claim(stage_root, RUN_KEY, payload)

    def unexpected_physical_check(*args: object) -> None:
        del args
        raise AssertionError("physical validation must follow logical validation")

    monkeypatch.setattr(
        WorkspaceCleanupEvidence,
        "_physical_claim_blocker",
        unexpected_physical_check,
    )

    decision = _evidence(stage_root, cache_root).decision(RUN_KEY, workspace_path)

    assert decision.reason == "workspace has an unresolved mutator fence"


@pytest.mark.parametrize("physical_state", ("symlink", "missing_git", "unregistered"))
def test_persisted_worktree_identity_retains_unsafe_checkout_shapes(
    tmp_path: Path,
    physical_state: str,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = _worktree_path(cache_root, RUN_KEY, physical_state)
    workspace_path.mkdir(parents=True)
    checkout = workspace_path / "checkout"
    if physical_state == "symlink":
        outside = tmp_path / "outside-checkout"
        outside.mkdir()
        checkout.symlink_to(outside, target_is_directory=True)
    else:
        checkout.mkdir()
        if physical_state == "unregistered":
            (checkout / ".git").write_text(
                "gitdir: /repo/.git/worktrees/workspace\n",
                encoding="utf-8",
            )
    _write_claim(stage_root, RUN_KEY, _worktree_payload(workspace_path))

    decision = _evidence(stage_root, cache_root).decision(RUN_KEY, workspace_path)

    assert decision.deletable is False
    assert "identity or disposal safety" in str(decision.reason)


def test_cleanup_evidence_reports_ambiguous_cache_keys(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    for family in ("snapshots", "workspaces"):
        workspace_path = cache_root / family / REPOSITORY_ID / RUN_KEY / "same-key"
        payload = (
            _snapshot_payload(workspace_path)
            if family == "snapshots"
            else _worktree_payload(workspace_path, generation=1)
        )
        _write_claim(
            stage_root,
            RUN_KEY,
            payload,
            f"workspace-state-{family}.json",
        )

    assert (
        _evidence(stage_root, cache_root).status_for_cache_key(RUN_KEY, "same-key")
        == "invalid"
    )


def _evidence(stage_root: Path, cache_root: Path) -> WorkspaceCleanupEvidence:
    return WorkspaceCleanupEvidence(
        stage_root,
        cache_root,
        REPOSITORY_ID,
        Path("/repo/.git"),
    )


def _snapshot_path(
    cache_root: Path,
    run_key: str = RUN_KEY,
    cache_key: str = "snapshot",
) -> Path:
    return cache_root / "snapshots" / REPOSITORY_ID / run_key / cache_key


def _worktree_path(cache_root: Path, run_key: str, cache_key: str) -> Path:
    return (
        cache_root / "review-workspaces" / REPOSITORY_ID / run_key / "build" / cache_key
    )


def _snapshot_payload(
    workspace_path: Path,
    run_key: str = RUN_KEY,
    cache_root: Path | None = None,
) -> dict[str, object]:
    return _base_payload(
        workspace_path,
        run_key,
        workspace_kind="snapshot",
        materialization="snapshot_checkout",
        generation=None,
        cache_root=cache_root,
    )


def _worktree_payload(
    workspace_path: Path,
    run_key: str = RUN_KEY,
    generation: int | None = 1,
) -> dict[str, object]:
    return _base_payload(
        workspace_path,
        run_key,
        workspace_kind="worktree",
        materialization="worktree_checkout",
        generation=generation,
    )


def _base_payload(
    workspace_path: Path,
    run_key: str,
    workspace_kind: str,
    materialization: str,
    generation: int | None,
    cache_root: Path | None = None,
) -> dict[str, object]:
    workspace: dict[str, object] = {
        "path": workspace_path.as_posix(),
        "effective_cwd": None,
        "cache_key": workspace_path.name,
        "materialization": materialization,
        "writable": True,
        "lineage_producer": False,
        "retention": "pending_cleanup",
        "retained_reason": None,
        "project_root_relative_path": ".",
    }
    if generation is not None:
        workspace["reuse_generation"] = generation
    result = (
        {
            "drift_scan_complete": True,
            "snapshot_drift_discarded": False,
            "changed_path_count": 0,
            "changed_paths": [],
            "changed_paths_truncated": False,
        }
        if workspace_kind == "snapshot"
        else {"lineage_produced": False}
    )
    return {
        "version": SCHEMA_VERSION,
        "run_id": f"{run_key}-id",
        "run_key_name": run_key,
        "workflow_name": "workflow",
        "workflow_signature": "e" * 64,
        "node_id": "build",
        "task_id": "alpha",
        "provider": "mock",
        "role": "reviewer" if workspace_kind == "worktree" else "executor",
        "round_num": 1,
        "audit_round_num": None,
        "status": "succeeded",
        "workspace_kind": workspace_kind,
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": WORKTREE_CONTRACT_PAYLOAD,
        "git": {
            "object_format": "sha1",
            "repo_id": REPOSITORY_ID,
            "run_base_commit": OID_A,
            "source_tree": OID_B,
            "git_top_level": "/repo",
            "active_git_dir": "/repo/.git",
            "common_git_dir": "/repo/.git",
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": OID_A,
            "tree": OID_B,
            "candidate_sequence": None,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": OID_A,
            "source_tree": OID_B,
            "candidate_sequence": None,
        },
        "workspace": workspace,
        "execution": {
            "cache_root": (
                cache_root or _payload_cache_root(workspace_path)
            ).as_posix(),
            "workspace_path": workspace_path.as_posix(),
            "checkout_root": (workspace_path / "checkout").as_posix(),
            "effective_cwd": None,
            "worktree_git_dir": (
                "/repo/.git/worktrees/workspace"
                if workspace_kind == "worktree"
                else None
            ),
        },
        "process_drain": {"status": "confirmed"},
        "result": result,
    }


def _payload_cache_root(workspace_path: Path) -> Path:
    for parent in workspace_path.parents:
        if parent.name in {"snapshots", "workspaces", "review-workspaces"}:
            return parent.parent
    return workspace_path.parents[3]


def _write_claim(
    stage_root: Path,
    run_key: str,
    payload: object,
    filename: str = "workspace-state.json",
) -> Path:
    workspace_kind = (
        payload.get("workspace_kind") if isinstance(payload, dict) else None
    )
    _write_plan(
        stage_root,
        run_key,
        workspace_kind=(
            workspace_kind if workspace_kind in {"snapshot", "worktree"} else "snapshot"
        ),
    )
    path = stage_root / run_key / "node" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_plan(
    stage_root: Path,
    run_key: str,
    node_id: str = "build",
    stage_path: str = "node",
    workspace_kind: str = "snapshot",
) -> None:
    run_dir = stage_root / run_key
    manifest = make_run_manifest(
        run_id=f"{run_key}-id",
        run_key_name=run_key,
        status="succeeded",
        workflow_name="workflow",
        workflow_signature="e" * 64,
    )
    payload = disabled_workspace_plan(stage_root.parent).model_dump(mode="json")
    payload.update(
        {
            "run_id": manifest.run_id,
            "run_key_name": run_key,
            "project_root": stage_root.parent.as_posix(),
            "context_root": run_dir.as_posix(),
            "manifest_root": (run_dir / "manifests").as_posix(),
            "workflow_name": manifest.workflow_name,
            "workflow_signature": manifest.workflow_signature,
            "effective_runtime_config_signature": (
                manifest.effective_runtime_config_signature
            ),
            "execution_order": [node_id],
        }
    )
    node = payload["nodes"][0]
    assert isinstance(node, dict)
    node["id"] = node_id
    node["render_plan_id"] = node_id
    node["workspace_policy"] = workspace_selection_record(
        kind=workspace_kind,
        lineage_producer=workspace_kind == "worktree",
    ).model_dump(mode="json")
    contract = node["artifact_contract"]
    assert isinstance(contract, dict)
    contract["stage_path"] = stage_path
    contract["output_path"] = f"{stage_path}/output.md"
    contract["log_path"] = f"{stage_path}/logs"
    contract["result_path"] = f"{stage_path}/output.md"
    render_plan = payload["render_plans"][0]
    assert isinstance(render_plan, dict)
    render_plan["render_plan_id"] = node_id
    render_plan["node_id"] = node_id
    plan = PreflightExecutionPlan.model_validate(payload)
    path = run_dir / "preflight" / "execution-plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    manifest_path = run_dir / "manifests" / "run.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        manifest.model_dump_json(exclude_none=True),
        encoding="utf-8",
    )
