from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from crewplane.cli.workspace_cleanup_evidence import WorkspaceCleanupEvidence
from tests.unit.cli.workspace_cleanup_evidence_support import (
    REPOSITORY_ID,
    RUN_KEY,
    collect_cleanup_evidence,
    snapshot_cache_path,
    snapshot_claim_payload,
    worktree_claim_payload,
    write_cleanup_claim,
)


def test_cleanup_evidence_reports_identity_status_and_contract_blockers(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    cases = {
        "outside": (
            tmp_path / "outside",
            snapshot_claim_payload(tmp_path / "outside", cache_root=cache_root),
            "cache family",
        ),
        "wrong-repository": (
            snapshot_cache_path(cache_root, "wrong-repository"),
            snapshot_claim_payload(snapshot_cache_path(cache_root, "wrong-repository")),
            "contradict repository",
        ),
        "planned": (
            snapshot_cache_path(cache_root, "planned"),
            snapshot_claim_payload(snapshot_cache_path(cache_root, "planned")),
            "workspace state is planned",
        ),
        "bad-contract": (
            snapshot_cache_path(cache_root, "bad-contract"),
            snapshot_claim_payload(snapshot_cache_path(cache_root, "bad-contract")),
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
        write_cleanup_claim(stage_root, run_key, payload)

    evidence = collect_cleanup_evidence(stage_root, cache_root)

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

    terminal_path = snapshot_cache_path(cache_root, "terminal-conflict", "terminal")
    first = snapshot_claim_payload(terminal_path, run_key="terminal-conflict")
    first["status"] = "planned"
    second = deepcopy(first)
    second["status"] = "materialized"
    write_cleanup_claim(
        stage_root, "terminal-conflict", first, "workspace-state-1.json"
    )
    write_cleanup_claim(
        stage_root, "terminal-conflict", second, "workspace-state-2.json"
    )

    kind_path = snapshot_cache_path(cache_root, "kind-conflict", "kind")
    snapshot = snapshot_claim_payload(kind_path, run_key="kind-conflict")
    worktree = worktree_claim_payload(kind_path, run_key="kind-conflict", generation=1)
    write_cleanup_claim(stage_root, "kind-conflict", snapshot, "workspace-state-1.json")
    write_cleanup_claim(stage_root, "kind-conflict", worktree, "workspace-state-2.json")

    evidence = collect_cleanup_evidence(stage_root, cache_root)

    assert "terminal outcome" in str(
        evidence.decision("terminal-conflict", terminal_path).reason
    )
    assert "malformed" in str(evidence.decision("kind-conflict", kind_path).reason)


def test_cleanup_evidence_rejects_invalid_snapshot_physical_evidence(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"

    duplicate_path = snapshot_cache_path(cache_root, "duplicate", "snapshot")
    duplicate_path.joinpath("checkout").mkdir(parents=True)
    duplicate = snapshot_claim_payload(duplicate_path, run_key="duplicate")
    write_cleanup_claim(stage_root, "duplicate", duplicate, "workspace-state-1.json")
    write_cleanup_claim(stage_root, "duplicate", duplicate, "workspace-state-2.json")

    missing_path = snapshot_cache_path(cache_root, "missing", "snapshot")
    write_cleanup_claim(
        stage_root,
        "missing",
        snapshot_claim_payload(missing_path, run_key="missing"),
    )

    git_path = snapshot_cache_path(cache_root, "git-admin", "snapshot")
    (git_path / "checkout" / ".git").mkdir(parents=True)
    write_cleanup_claim(
        stage_root,
        "git-admin",
        snapshot_claim_payload(git_path, run_key="git-admin"),
    )

    evidence = collect_cleanup_evidence(stage_root, cache_root)

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
            payload = worktree_claim_payload(
                workspace_path,
                run_key=run_key,
                generation=generation,
            )
            write_cleanup_claim(
                stage_root,
                run_key,
                payload,
                f"workspace-state-{index}.json",
            )

    evidence = collect_cleanup_evidence(stage_root, cache_root)

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
    payload = worktree_claim_payload(workspace_path, generation=None)
    payload["workspace_mutator"] = {"status": "unresolved"}
    write_cleanup_claim(stage_root, RUN_KEY, payload)

    def unexpected_physical_check(*args: object) -> None:
        del args
        raise AssertionError("physical validation must follow logical validation")

    monkeypatch.setattr(
        WorkspaceCleanupEvidence,
        "_physical_claim_blocker",
        unexpected_physical_check,
    )

    decision = collect_cleanup_evidence(stage_root, cache_root).decision(
        RUN_KEY, workspace_path
    )

    assert decision.reason == "workspace has an unresolved mutator fence"


@pytest.mark.parametrize("physical_state", ["symlink", "missing_git", "unregistered"])
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
    write_cleanup_claim(stage_root, RUN_KEY, worktree_claim_payload(workspace_path))

    decision = collect_cleanup_evidence(stage_root, cache_root).decision(
        RUN_KEY, workspace_path
    )

    assert decision.deletable is False
    assert "identity or disposal safety" in str(decision.reason)


def test_cleanup_evidence_reports_ambiguous_cache_keys(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    for family in ("snapshots", "workspaces"):
        workspace_path = cache_root / family / REPOSITORY_ID / RUN_KEY / "same-key"
        payload = (
            snapshot_claim_payload(workspace_path)
            if family == "snapshots"
            else worktree_claim_payload(workspace_path, generation=1)
        )
        write_cleanup_claim(
            stage_root,
            RUN_KEY,
            payload,
            f"workspace-state-{family}.json",
        )

    assert (
        collect_cleanup_evidence(stage_root, cache_root).status_for_cache_key(
            RUN_KEY, "same-key"
        )
        == "invalid"
    )


def _worktree_path(cache_root: Path, run_key: str, cache_key: str) -> Path:
    return (
        cache_root / "review-workspaces" / REPOSITORY_ID / run_key / "build" / cache_key
    )
