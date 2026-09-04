from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crewplane.cli.workspace_cleanup.evidence_claims import (
    WorkspaceClaim,
    claim_has_pending_ref_cleanup,
    claim_retention,
    load_authoritative_plan,
    load_workspace_claim,
)
from crewplane.cli.workspace_cleanup.evidence_discovery import (
    planned_stage_directories,
    scan_claim_paths,
    scan_dedicated_ref_evidence,
)
from crewplane.cli.workspace_cleanup.evidence_validation import (
    WorkspaceClaimValidator,
    path_is_absent,
)
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.runtime.workspace.cleanup import AbsentWorkspaceStateProjection


@dataclass(frozen=True)
class WorkspaceCleanupEvidenceDecision:
    status: str | None
    deletable: bool
    reason: str | None = None
    state_paths: tuple[Path, ...] = ()
    expected_worktree_git_dir: Path | None = None


class WorkspaceCleanupEvidence:
    def __init__(
        self,
        stage_root: Path,
        cache_root: Path,
        repository_id: str,
        expected_common_git_dir: Path,
    ) -> None:
        self._cache_root = cache_root.resolve(strict=False)
        self._repository_id = repository_id
        self._expected_common_git_dir = expected_common_git_dir.resolve(strict=False)
        self._claims: dict[tuple[str, Path], list[WorkspaceClaim]] = {}
        self._dedicated_ref_run_keys: set[str] = set()
        self._corrupt_runs: set[str] = set()
        self._collection_invalid = False
        self._validator = WorkspaceClaimValidator(
            self._cache_root,
            self._repository_id,
            self._expected_common_git_dir,
        )
        self._collect(stage_root)

    def decision(
        self,
        run_key_name: str,
        workspace_path: Path,
    ) -> WorkspaceCleanupEvidenceDecision:
        path = workspace_path.resolve(strict=False)
        claims = tuple(self._claims.get((run_key_name, path), ()))
        if not claims:
            if self._collection_invalid or run_key_name in self._corrupt_runs:
                return WorkspaceCleanupEvidenceDecision(
                    status="invalid",
                    deletable=False,
                    reason="workspace evidence for the run is malformed",
                )
            return WorkspaceCleanupEvidenceDecision(None, True)
        reason = self._claim_blocker(run_key_name, path, claims)
        if reason is not None:
            return WorkspaceCleanupEvidenceDecision(
                status="invalid",
                deletable=False,
                reason=reason,
                state_paths=tuple(claim.state_path for claim in claims),
            )
        highest = _highest_generation_claim(claims)
        return WorkspaceCleanupEvidenceDecision(
            status=str(highest.payload["status"]),
            deletable=True,
            state_paths=tuple(claim.state_path for claim in claims),
            expected_worktree_git_dir=highest.worktree_git_dir,
        )

    def status_for_cache_key(self, run_key_name: str, cache_key: str) -> str | None:
        paths = {
            path
            for claim_run_key, path in self._claims
            if claim_run_key == run_key_name and path.name == cache_key
        }
        if len(paths) == 1:
            return self.decision(run_key_name, paths.pop()).status
        if (
            len(paths) > 1
            or self._collection_invalid
            or run_key_name in self._corrupt_runs
        ):
            return "invalid"
        return None

    def ref_cleanup_run_keys(self) -> tuple[str, ...]:
        claimed_run_keys = {run_key for run_key, _path in self._claims}
        eligible_run_keys = claimed_run_keys | self._dedicated_ref_run_keys
        return tuple(sorted(eligible_run_keys - self._corrupt_runs))

    def absent_state_projections(
        self,
    ) -> tuple[AbsentWorkspaceStateProjection, ...]:
        projections: list[AbsentWorkspaceStateProjection] = []
        for (run_key_name, workspace_path), claim_values in sorted(
            self._claims.items(),
            key=lambda item: (item[0][0], item[0][1].as_posix()),
        ):
            claims = tuple(claim_values)
            if self._absent_projection_is_eligible(
                run_key_name,
                workspace_path,
                claims,
            ):
                highest = _highest_generation_claim(claims)
                projections.append(
                    AbsentWorkspaceStateProjection(
                        run_key_name=run_key_name,
                        workspace_path=workspace_path,
                        status=str(highest.payload["status"]),
                        state_paths=tuple(claim.state_path for claim in claims),
                    )
                )
        return tuple(projections)

    def _collect(self, stage_root: Path) -> None:
        if stage_root.is_symlink():
            self._collection_invalid = True
            return
        if not stage_root.exists():
            return
        if not stage_root.is_dir():
            self._collection_invalid = True
            return
        for run_dir in sorted(stage_root.iterdir()):
            self._collect_run(run_dir)

    def _collect_run(self, run_dir: Path) -> None:
        if run_dir.is_symlink():
            self._corrupt_runs.add(run_dir.name)
            return
        if not run_dir.is_dir():
            return
        run_key_name = run_dir.name
        plan_load = load_authoritative_plan(run_dir, run_key_name)
        if plan_load.invalid:
            self._corrupt_runs.add(run_key_name)
        plan = plan_load.plan
        stage_dirs: tuple[tuple[Path, str], ...] = ()
        if plan is not None:
            planned_stages = planned_stage_directories(run_dir, plan)
            stage_dirs = planned_stages.directories
            if planned_stages.invalid:
                self._corrupt_runs.add(run_key_name)
        ref_scan = scan_dedicated_ref_evidence(run_dir, stage_dirs)
        if ref_scan.invalid:
            self._corrupt_runs.add(run_key_name)
        if ref_scan.paths:
            self._dedicated_ref_run_keys.add(run_key_name)
        if plan is not None:
            self._collect_run_claims(run_dir, run_key_name, stage_dirs, plan)

    def _collect_run_claims(
        self,
        run_dir: Path,
        run_key_name: str,
        stage_dirs: tuple[tuple[Path, str], ...],
        plan: PreflightExecutionPlan,
    ) -> None:
        claim_scan = scan_claim_paths(run_dir, stage_dirs)
        if claim_scan.invalid:
            self._corrupt_runs.add(run_key_name)
        for candidate in claim_scan.claims:
            claim_load = load_workspace_claim(
                candidate.state_path,
                run_key_name,
                candidate.expected_node_id,
                plan,
                self._cache_root,
            )
            if claim_load.invalid:
                self._corrupt_runs.add(run_key_name)
            if claim_load.claim is not None:
                claim = claim_load.claim
                key = (claim.run_key_name, claim.workspace_path)
                self._claims.setdefault(key, []).append(claim)

    def _claim_blocker(
        self,
        run_key_name: str,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
    ) -> str | None:
        evidence_blocker = self._claim_evidence_blocker(
            run_key_name,
            workspace_path,
            claims,
        )
        if evidence_blocker is not None:
            return evidence_blocker
        highest = _highest_generation_claim(claims)
        if claim_retention(highest) == "deleted" and not path_is_absent(workspace_path):
            return "workspace path reappeared after deletion; ownership is unknown"
        return self._physical_claim_blocker(workspace_path, claims)

    def _claim_evidence_blocker(
        self,
        run_key_name: str,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
    ) -> str | None:
        return self._validator.evidence_blocker(
            run_key_name,
            workspace_path,
            claims,
            run_key_name in self._corrupt_runs,
        )

    def _physical_claim_blocker(
        self,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
    ) -> str | None:
        return self._validator.physical_blocker(workspace_path, claims)

    def _absent_projection_is_eligible(
        self,
        run_key_name: str,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
    ) -> bool:
        if not path_is_absent(workspace_path):
            return False
        if self._claim_evidence_blocker(run_key_name, workspace_path, claims):
            return False
        if not self._validator.absent_physical_evidence_is_coherent(
            workspace_path,
            claims,
        ):
            return False
        return not (
            all(claim_retention(claim) == "deleted" for claim in claims)
            and not any(claim_has_pending_ref_cleanup(claim) for claim in claims)
        )


def _highest_generation_claim(
    claims: tuple[WorkspaceClaim, ...],
) -> WorkspaceClaim:
    return max(claims, key=lambda claim: claim.generation or 0)
