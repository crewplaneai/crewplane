from __future__ import annotations

import subprocess
from pathlib import Path

from crewplane.artifacts.workspace.state.contracts import (
    workspace_state_contract_errors,
)
from crewplane.runtime.workspace.worktree.cleanup import (
    registered_worktree_paths,
    verify_registered_worktree_cleanup_path,
)
from crewplane.runtime.workspace.worktree.refs import safe_file_component

from .evidence_claims import WorkspaceClaim

_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class WorkspaceClaimValidator:
    def __init__(
        self,
        cache_root: Path,
        repository_id: str,
        expected_common_git_dir: Path,
    ) -> None:
        self._cache_root = cache_root
        self._repository_id = repository_id
        self._expected_common_git_dir = expected_common_git_dir

    def evidence_blocker(
        self,
        run_key_name: str,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
        run_is_corrupt: bool,
    ) -> str | None:
        blocker = self._identity_evidence_blocker(
            run_key_name,
            workspace_path,
            claims,
            run_is_corrupt,
        )
        if blocker is not None:
            return blocker
        blocker = _claim_state_blocker(claims)
        if blocker is not None:
            return blocker
        blocker = self._placement_evidence_blocker(
            run_key_name,
            workspace_path,
            claims,
        )
        if blocker is not None:
            return blocker
        return self._variant_evidence_blocker(claims[0].workspace_kind, claims)

    def physical_blocker(
        self,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
    ) -> str | None:
        match claims[0].workspace_kind:
            case "snapshot":
                return _snapshot_physical_blocker(workspace_path)
            case "worktree":
                return self._worktree_physical_blocker(workspace_path, claims)
            case _:
                return "workspace claim has an unknown materialization kind"

    def absent_physical_evidence_is_coherent(
        self,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
    ) -> bool:
        if claims[0].workspace_kind == "snapshot":
            return True
        expected_git_dir = claims[0].worktree_git_dir
        if expected_git_dir is None or not path_is_absent(expected_git_dir):
            return False
        try:
            registered_paths = registered_worktree_paths(self._expected_common_git_dir)
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            return False
        checkout_root = (workspace_path / "checkout").resolve(strict=False)
        return checkout_root not in registered_paths

    def _identity_evidence_blocker(
        self,
        run_key_name: str,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
        run_is_corrupt: bool,
    ) -> str | None:
        if run_is_corrupt:
            return "workspace evidence for the run is malformed"
        if self._claims_contradict_identity(run_key_name, workspace_path, claims):
            return "workspace claims contradict repository, run, or path identity"
        return None

    def _placement_evidence_blocker(
        self,
        run_key_name: str,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
    ) -> str | None:
        if len({claim.workspace_kind for claim in claims}) != 1:
            return "workspace claims disagree about materialization kind"
        if any(
            not self._path_matches_cache_family(run_key_name, workspace_path, claim)
            for claim in claims
        ):
            return "workspace path does not match its cache family and run"
        return None

    def _claims_contradict_identity(
        self,
        run_key_name: str,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
    ) -> bool:
        return any(
            claim.repository_id != self._repository_id
            or claim.common_git_dir != self._expected_common_git_dir
            or claim.run_key_name != run_key_name
            or claim.workspace_path != workspace_path
            for claim in claims
        )

    def _path_matches_cache_family(
        self,
        run_key_name: str,
        workspace_path: Path,
        claim: WorkspaceClaim,
    ) -> bool:
        try:
            relative = workspace_path.relative_to(self._cache_root)
        except ValueError:
            return False
        owner = (self._repository_id, run_key_name)
        match claim.workspace_kind:
            case "snapshot":
                return _snapshot_cache_family_matches(relative.parts, owner)
            case "worktree":
                return _worktree_cache_family_matches(relative.parts, owner, claim)
            case _:
                return False

    def _worktree_physical_blocker(
        self,
        workspace_path: Path,
        claims: tuple[WorkspaceClaim, ...],
    ) -> str | None:
        try:
            expected_git_dirs = {claim.worktree_git_dir for claim in claims}
            expected_git_dir = next(iter(expected_git_dirs))
            if expected_git_dir is None:
                return "worktree claim lacks exact Git directory identity"
            verify_registered_worktree_cleanup_path(
                workspace_path,
                self._expected_common_git_dir,
                expected_git_dir,
            )
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            return "worktree checkout identity or disposal safety is unverifiable"
        return None

    @staticmethod
    def _variant_evidence_blocker(
        kind: str,
        claims: tuple[WorkspaceClaim, ...],
    ) -> str | None:
        match kind:
            case "snapshot":
                return _snapshot_evidence_blocker(claims)
            case "worktree":
                return _worktree_evidence_blocker(claims)
            case _:
                return "workspace claim has an unknown materialization kind"


def path_is_absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _claim_has_unresolved_mutator(claim: WorkspaceClaim) -> bool:
    mutator = claim.payload.get("workspace_mutator")
    return isinstance(mutator, dict) and mutator.get("status") == "unresolved"


def _claim_state_blocker(claims: tuple[WorkspaceClaim, ...]) -> str | None:
    if any(_claim_has_unresolved_mutator(claim) for claim in claims):
        return "workspace has an unresolved mutator fence"
    status_blocker = _status_blocker(claims)
    if status_blocker is not None:
        return status_blocker
    if any(
        workspace_state_contract_errors(claim.payload, "cleanup") for claim in claims
    ):
        return "workspace claim lacks required terminal hardening evidence"
    return None


def _status_blocker(claims: tuple[WorkspaceClaim, ...]) -> str | None:
    statuses = {claim.payload.get("status") for claim in claims}
    nonterminal_statuses = statuses - _TERMINAL_STATUSES
    if not nonterminal_statuses:
        return None
    if len(nonterminal_statuses) == 1:
        return f"workspace state is {nonterminal_statuses.pop()}"
    return "workspace claims disagree about terminal outcome"


def _snapshot_evidence_blocker(
    claims: tuple[WorkspaceClaim, ...],
) -> str | None:
    if len(claims) != 1 or claims[0].generation is not None:
        return "snapshot workspace has unexpected generation claims"
    return None


def _worktree_evidence_blocker(
    claims: tuple[WorkspaceClaim, ...],
) -> str | None:
    generations = [claim.generation for claim in claims]
    if any(generation is None or generation < 1 for generation in generations):
        return "worktree claim lacks a positive reuse generation"
    integer_generations = [
        generation for generation in generations if generation is not None
    ]
    if len(integer_generations) != len(set(integer_generations)):
        return "worktree has duplicate generation claims"
    if sorted(integer_generations) != list(range(1, max(integer_generations) + 1)):
        return "worktree generation claims are not coherent and monotonic"
    expected_git_dirs = {claim.worktree_git_dir for claim in claims}
    if len(expected_git_dirs) != 1 or None in expected_git_dirs:
        return "worktree claim lacks exact Git directory identity"
    return None


def _snapshot_cache_family_matches(
    parts: tuple[str, ...],
    owner: tuple[str, str],
) -> bool:
    return len(parts) == 4 and parts[:3] == ("snapshots", *owner)


def _worktree_cache_family_matches(
    parts: tuple[str, ...],
    owner: tuple[str, str],
    claim: WorkspaceClaim,
) -> bool:
    role = claim.payload.get("role")
    if role == "executor":
        return len(parts) == 4 and parts[:3] == ("workspaces", *owner)
    if role == "reviewer":
        return (
            len(parts) == 5
            and parts[:3] == ("review-workspaces", *owner)
            and parts[3] == safe_file_component(str(claim.payload["node_id"]))
        )
    return False


def _snapshot_physical_blocker(workspace_path: Path) -> str | None:
    checkout_root = workspace_path / "checkout"
    if (
        workspace_path.is_symlink()
        or not workspace_path.is_dir()
        or checkout_root.is_symlink()
        or not checkout_root.is_dir()
    ):
        return "snapshot workspace is not a real checkout directory"
    if (checkout_root / ".git").exists():
        return "snapshot workspace unexpectedly contains Git administration"
    return None
