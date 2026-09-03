from __future__ import annotations

import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from crewplane.artifacts.workspace.state.contracts import (
    workspace_state_contract_errors,
)
from crewplane.core.execution_state import RunManifest
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
)
from crewplane.core.workflow.keywords import RESERVED_RUN_ROOT_NAMES
from crewplane.runtime.workspace.worktree.cleanup import (
    registered_worktree_paths,
    verify_registered_worktree_cleanup_path,
)

_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


@dataclass(frozen=True)
class WorkspaceCleanupEvidenceDecision:
    status: str | None
    deletable: bool
    reason: str | None = None
    state_paths: tuple[Path, ...] = ()
    expected_worktree_git_dir: Path | None = None


@dataclass(frozen=True)
class _WorkspaceClaim:
    state_path: Path
    payload: dict[str, object]
    repository_id: str
    run_key_name: str
    workspace_path: Path
    workspace_kind: str
    common_git_dir: Path
    worktree_git_dir: Path | None
    generation: int | None


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
        self._claims: dict[tuple[str, Path], list[_WorkspaceClaim]] = {}
        self._dedicated_ref_run_keys: set[str] = set()
        self._corrupt_runs: set[str] = set()
        self._collection_invalid = False
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
        highest = max(claims, key=lambda claim: claim.generation or 0)
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
    ) -> tuple[tuple[str, Path, str, tuple[Path, ...]], ...]:
        projections: list[tuple[str, Path, str, tuple[Path, ...]]] = []
        for (run_key_name, workspace_path), claim_values in sorted(
            self._claims.items(),
            key=lambda item: (item[0][0], item[0][1].as_posix()),
        ):
            claims = tuple(claim_values)
            if not _path_is_absent(workspace_path):
                continue
            if self._claim_evidence_blocker(run_key_name, workspace_path, claims):
                continue
            if not self._absent_physical_evidence_is_coherent(workspace_path, claims):
                continue
            if all(
                _claim_retention(claim) == "deleted" for claim in claims
            ) and not any(_claim_has_pending_ref_cleanup(claim) for claim in claims):
                continue
            highest = max(claims, key=lambda claim: claim.generation or 0)
            projections.append(
                (
                    run_key_name,
                    workspace_path,
                    str(highest.payload["status"]),
                    tuple(claim.state_path for claim in claims),
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
            if run_dir.is_symlink():
                self._corrupt_runs.add(run_dir.name)
                continue
            if not run_dir.is_dir():
                continue
            run_key_name = run_dir.name
            plan = self._load_authoritative_plan(run_dir, run_key_name)
            stage_dirs = (
                self._planned_stage_dirs(run_dir, run_key_name, plan)
                if plan is not None
                else ()
            )
            if self._has_dedicated_ref_evidence(
                run_dir,
                run_key_name,
                stage_dirs,
            ):
                self._dedicated_ref_run_keys.add(run_key_name)
            if plan is None:
                continue
            for state_path, expected_node_id in self._claim_paths(
                run_dir,
                run_key_name,
                stage_dirs,
            ):
                claim = self._load_claim(
                    state_path,
                    run_key_name,
                    expected_node_id,
                    plan,
                )
                if claim is None:
                    continue
                key = (claim.run_key_name, claim.workspace_path)
                self._claims.setdefault(key, []).append(claim)

    def _has_dedicated_ref_evidence(
        self,
        run_dir: Path,
        run_key_name: str,
        stage_dirs: tuple[tuple[Path, str], ...],
    ) -> bool:
        found = False
        evidence_dirs = (run_dir / "logs", *(path for path, _node_id in stage_dirs))
        for evidence_dir in evidence_dirs:
            if not evidence_dir.exists():
                continue
            if (
                _path_has_symlink_component(evidence_dir, run_dir)
                or evidence_dir.is_symlink()
                or not evidence_dir.is_dir()
            ):
                self._corrupt_runs.add(run_key_name)
                continue
            try:
                paths = tuple(evidence_dir.iterdir())
            except OSError:
                self._corrupt_runs.add(run_key_name)
                continue
            evidence_paths = [
                path
                for path in paths
                if path.name.startswith("workspace-temporary-refs-")
                and path.name.endswith(".json")
            ]
            if any(not _single_link_regular_file(path) for path in evidence_paths):
                self._corrupt_runs.add(run_key_name)
                continue
            found = found or bool(evidence_paths)
        return found

    def _claim_paths(
        self,
        run_dir: Path,
        run_key_name: str,
        stage_dirs: tuple[tuple[Path, str], ...],
    ) -> tuple[tuple[Path, str], ...]:
        candidates: list[tuple[Path, str]] = []
        for stage_dir, expected_node_id in stage_dirs:
            if _path_has_symlink_component(stage_dir, run_dir):
                self._corrupt_runs.add(run_key_name)
                continue
            if not stage_dir.is_dir():
                continue
            try:
                entries = tuple(stage_dir.iterdir())
            except OSError:
                self._corrupt_runs.add(run_key_name)
                continue
            for path in entries:
                if not _claim_file_name(path.name):
                    continue
                if not _single_link_regular_file(path):
                    self._corrupt_runs.add(run_key_name)
                    continue
                candidates.append((path, expected_node_id))
        return tuple(sorted(candidates, key=lambda item: item[0].as_posix()))

    def _planned_stage_dirs(
        self,
        run_dir: Path,
        run_key_name: str,
        plan: PreflightExecutionPlan,
    ) -> tuple[tuple[Path, str], ...]:
        stage_dirs: list[tuple[Path, str]] = []
        for node in plan.nodes:
            stage_path = node.artifact_contract.stage_path
            if stage_path is None or not _safe_stage_path(stage_path):
                self._corrupt_runs.add(run_key_name)
                return ()
            stage_dirs.append((run_dir / stage_path, node.id))
        return tuple(stage_dirs)

    def _load_authoritative_plan(
        self,
        run_dir: Path,
        run_key_name: str,
    ) -> PreflightExecutionPlan | None:
        plan_path = run_dir / "preflight" / "execution-plan.json"
        manifest_path = run_dir / "manifests" / "run.json"
        files_are_safe = self._authoritative_file_is_safe(
            run_dir,
            plan_path,
        ) and self._authoritative_file_is_safe(run_dir, manifest_path)
        if not files_are_safe:
            self._corrupt_runs.add(run_key_name)
            return None
        try:
            plan = PreflightExecutionPlan.model_validate_json(
                plan_path.read_text(encoding="utf-8")
            )
            manifest = RunManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, UnicodeDecodeError):
            self._corrupt_runs.add(run_key_name)
            return None
        if not _plan_manifest_identity_matches(
            plan,
            manifest,
            run_key_name,
            run_dir,
        ):
            self._corrupt_runs.add(run_key_name)
            return None
        return plan

    @staticmethod
    def _authoritative_file_is_safe(run_dir: Path, path: Path) -> bool:
        try:
            path.lstat()
        except OSError:
            return False
        return not _path_has_symlink_component(
            path,
            run_dir,
        ) and _single_link_regular_file(path)

    def _load_claim(
        self,
        state_path: Path,
        directory_run_key: str,
        expected_node_id: str,
        plan: PreflightExecutionPlan,
    ) -> _WorkspaceClaim | None:
        if not _single_link_regular_file(state_path):
            self._corrupt_runs.add(directory_run_key)
            return None
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            self._corrupt_runs.add(directory_run_key)
            return None
        if not isinstance(payload, dict):
            self._corrupt_runs.add(directory_run_key)
            return None
        run_key_name = payload.get("run_key_name")
        if not (
            isinstance(run_key_name, str)
            and payload.get("run_id") == plan.run_id
            and run_key_name == plan.run_key_name == directory_run_key
            and payload.get("workflow_name") == plan.workflow_name
            and payload.get("workflow_signature") == plan.workflow_signature
            and payload.get("node_id") == expected_node_id
        ):
            self._corrupt_runs.add(directory_run_key)
            return None
        execution = payload.get("execution")
        workspace_path_value = (
            execution.get("workspace_path") if isinstance(execution, dict) else None
        )
        if not isinstance(workspace_path_value, str):
            workspace = payload.get("workspace")
            hydrated = (
                isinstance(workspace, dict)
                and workspace.get("retention") == "not_applicable"
                and workspace.get("retained_reason") == "hydrated_resume"
                and not workspace_state_contract_errors(payload, "duplicate_skip")
            )
            if not hydrated:
                self._corrupt_runs.add(directory_run_key)
            return None
        workspace_path = _normalized_absolute_path(workspace_path_value)
        if workspace_path is None:
            self._corrupt_runs.add(directory_run_key)
            return None
        git = payload.get("git")
        workspace = payload.get("workspace")
        execution = payload.get("execution")
        if not (
            isinstance(run_key_name, str)
            and run_key_name == directory_run_key
            and isinstance(git, dict)
            and isinstance(workspace, dict)
            and isinstance(git.get("repo_id"), str)
            and isinstance(git.get("common_git_dir"), str)
            and isinstance(payload.get("workspace_kind"), str)
        ):
            self._corrupt_runs.add(directory_run_key)
            return None
        policy = next(
            (
                node.workspace_policy
                for node in plan.nodes
                if node.id == expected_node_id
            ),
            None,
        )
        if not _claim_matches_workspace_policy(payload, policy):
            self._corrupt_runs.add(directory_run_key)
            return None
        generation = workspace.get("reuse_generation")
        if not isinstance(generation, int) or isinstance(generation, bool):
            generation = None
        claim = _WorkspaceClaim(
            state_path=state_path,
            payload=payload,
            repository_id=str(git["repo_id"]),
            run_key_name=run_key_name,
            workspace_path=workspace_path,
            workspace_kind=str(payload["workspace_kind"]),
            common_git_dir=Path(str(git["common_git_dir"])).resolve(strict=False),
            worktree_git_dir=(
                Path(str(execution["worktree_git_dir"])).resolve(strict=False)
                if isinstance(execution, dict)
                and isinstance(execution.get("worktree_git_dir"), str)
                else None
            ),
            generation=generation,
        )
        if not self._claim_placement_is_coherent(claim):
            self._corrupt_runs.add(directory_run_key)
            return None
        return claim

    def _claim_blocker(
        self,
        run_key_name: str,
        workspace_path: Path,
        claims: tuple[_WorkspaceClaim, ...],
    ) -> str | None:
        evidence_blocker = self._claim_evidence_blocker(
            run_key_name,
            workspace_path,
            claims,
        )
        if evidence_blocker is not None:
            return evidence_blocker
        highest = max(claims, key=lambda claim: claim.generation or 0)
        if _claim_retention(highest) == "deleted" and not _path_is_absent(
            workspace_path
        ):
            return "workspace path reappeared after deletion; ownership is unknown"
        return self._physical_claim_blocker(workspace_path, claims)

    def _claim_evidence_blocker(
        self,
        run_key_name: str,
        workspace_path: Path,
        claims: tuple[_WorkspaceClaim, ...],
    ) -> str | None:
        if run_key_name in self._corrupt_runs:
            return "workspace evidence for the run is malformed"
        if any(
            claim.repository_id != self._repository_id
            or claim.common_git_dir != self._expected_common_git_dir
            or claim.run_key_name != run_key_name
            or claim.workspace_path != workspace_path
            for claim in claims
        ):
            return "workspace claims contradict repository, run, or path identity"
        statuses = {claim.payload.get("status") for claim in claims}
        if any(
            isinstance(mutator := claim.payload.get("workspace_mutator"), dict)
            and mutator.get("status") == "unresolved"
            for claim in claims
        ):
            return "workspace has an unresolved mutator fence"
        nonterminal_statuses = statuses - _TERMINAL_STATUSES
        if nonterminal_statuses:
            if len(nonterminal_statuses) == 1:
                return f"workspace state is {nonterminal_statuses.pop()}"
            return "workspace claims disagree about terminal outcome"
        contract_errors = [
            errors
            for claim in claims
            if (errors := workspace_state_contract_errors(claim.payload, "cleanup"))
        ]
        if contract_errors:
            return "workspace claim lacks required terminal hardening evidence"
        kinds = {claim.workspace_kind for claim in claims}
        if len(kinds) != 1:
            return "workspace claims disagree about materialization kind"
        if any(
            not self._path_matches_claim_cache_family(
                run_key_name,
                workspace_path,
                claim,
            )
            for claim in claims
        ):
            return "workspace path does not match its cache family and run"
        if next(iter(kinds)) == "snapshot":
            if len(claims) != 1 or claims[0].generation is not None:
                return "snapshot workspace has unexpected generation claims"
            return None
        if next(iter(kinds)) != "worktree":
            return "workspace claim has an unknown materialization kind"
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

    def _physical_claim_blocker(
        self,
        workspace_path: Path,
        claims: tuple[_WorkspaceClaim, ...],
    ) -> str | None:
        if claims[0].workspace_kind == "snapshot":
            checkout_root = workspace_path / "checkout"
            if (
                workspace_path.is_symlink()
                or not workspace_path.is_dir()
                or checkout_root.is_symlink()
                or not checkout_root.is_dir()
            ):
                return "snapshot workspace is not a real checkout directory"
            if (workspace_path / "checkout" / ".git").exists():
                return "snapshot workspace unexpectedly contains Git administration"
            return None
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

    def _claim_placement_is_coherent(self, claim: _WorkspaceClaim) -> bool:
        workspace = claim.payload.get("workspace")
        execution = claim.payload.get("execution")
        if not isinstance(workspace, dict) or not isinstance(execution, dict):
            return False
        checkout_root = claim.workspace_path / "checkout"
        if not _optional_path_matches(
            workspace.get("path"),
            claim.workspace_path,
        ):
            return False
        if workspace.get("cache_key") != claim.workspace_path.name:
            return False
        if not _optional_path_matches(workspace.get("cache_root"), self._cache_root):
            return False
        if not _optional_path_matches(
            workspace.get("checkout_root"),
            checkout_root,
        ):
            return False
        if not _required_path_matches(execution.get("cache_root"), self._cache_root):
            return False
        if not _required_path_matches(
            execution.get("workspace_path"),
            claim.workspace_path,
        ):
            return False
        if not _required_path_matches(
            execution.get("checkout_root"),
            checkout_root,
        ):
            return False
        project_relative = workspace.get("project_root_relative_path")
        if not isinstance(project_relative, str):
            return False
        relative_path = Path(project_relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return False
        expected_cwd = (checkout_root / relative_path).resolve(strict=False)
        return _optional_path_matches(
            workspace.get("effective_cwd"),
            expected_cwd,
        ) and _optional_path_matches(
            execution.get("effective_cwd"),
            expected_cwd,
        )

    def _path_matches_claim_cache_family(
        self,
        run_key_name: str,
        workspace_path: Path,
        claim: _WorkspaceClaim,
    ) -> bool:
        try:
            relative = workspace_path.relative_to(self._cache_root)
        except ValueError:
            return False
        parts = relative.parts
        owner = (self._repository_id, run_key_name)
        if claim.workspace_kind == "snapshot":
            return len(parts) == 4 and parts[:3] == ("snapshots", *owner)
        workspace = claim.payload.get("workspace")
        lineage_producer = (
            workspace.get("lineage_producer") if isinstance(workspace, dict) else None
        )
        if lineage_producer is True:
            return len(parts) == 4 and parts[:3] == ("workspaces", *owner)
        if lineage_producer is False:
            return (
                len(parts) == 5
                and parts[:3] == ("review-workspaces", *owner)
                and parts[3] == claim.payload.get("node_id")
            )
        return False

    def _absent_physical_evidence_is_coherent(
        self,
        workspace_path: Path,
        claims: tuple[_WorkspaceClaim, ...],
    ) -> bool:
        if claims[0].workspace_kind == "snapshot":
            return True
        expected_git_dir = claims[0].worktree_git_dir
        if expected_git_dir is None or not _path_is_absent(expected_git_dir):
            return False
        try:
            registered_paths = registered_worktree_paths(self._expected_common_git_dir)
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            return False
        return (workspace_path / "checkout").resolve(
            strict=False
        ) not in registered_paths


def _plan_manifest_identity_matches(
    plan: PreflightExecutionPlan,
    manifest: RunManifest,
    run_key_name: str,
    run_dir: Path,
) -> bool:
    return (
        manifest.preflight_plan_path == "preflight/execution-plan.json"
        and Path(plan.context_root).resolve(strict=False)
        == run_dir.resolve(strict=False)
        and Path(plan.manifest_root).resolve(strict=False)
        == (run_dir / "manifests").resolve(strict=False)
        and plan.run_id == manifest.run_id
        and plan.run_key_name == manifest.run_key_name == run_key_name
        and plan.plan_schema_version == manifest.plan_schema_version
        and plan.workflow_name == manifest.workflow_name
        and plan.workflow_signature == manifest.workflow_signature
        and plan.effective_runtime_config_signature
        == manifest.effective_runtime_config_signature
    )


def _claim_file_name(name: str) -> bool:
    return name == "workspace-state.json" or (
        name.endswith(".json")
        and (
            name.startswith("workspace-state-")
            or name.startswith("workspace-reuse-claim-")
        )
    )


def _claim_matches_workspace_policy(
    payload: dict[str, object],
    policy: WorkspaceSelectionRecord | None,
) -> bool:
    workspace = payload.get("workspace")
    if policy is None or not policy.enabled or not isinstance(workspace, dict):
        return False
    expected_lineage_producer = (
        policy.declaration_kind == "worktree" and payload.get("role") == "executor"
    )
    return (
        payload.get("workspace_kind") == policy.declaration_kind
        and payload.get("logical_worktree_name") == policy.logical_worktree_name
        and payload.get("clean_start") == policy.clean_start
        and payload.get("worktree_contract")
        == policy.worktree_contract.model_dump(mode="json")
        and workspace.get("materialization") == policy.materialization
        and workspace.get("writable") is policy.writable
        and workspace.get("lineage_producer") is expected_lineage_producer
    )


def _safe_stage_path(value: str) -> bool:
    path = Path(value)
    return (
        bool(value.strip())
        and not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.parts[0] not in RESERVED_RUN_ROOT_NAMES
    )


def _single_link_regular_file(path: Path) -> bool:
    try:
        file_stat = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(file_stat.st_mode) and file_stat.st_nlink == 1


def _normalized_absolute_path(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _required_path_matches(value: object, expected: Path) -> bool:
    return _normalized_absolute_path(value) == expected.resolve(strict=False)


def _optional_path_matches(value: object, expected: Path) -> bool:
    return value is None or _required_path_matches(value, expected)


def _path_is_absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _path_has_symlink_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(mode):
            return True
    return False


def _claim_retention(claim: _WorkspaceClaim) -> object:
    workspace = claim.payload.get("workspace")
    return workspace.get("retention") if isinstance(workspace, dict) else None


def _claim_has_pending_ref_cleanup(claim: _WorkspaceClaim) -> bool:
    publication = claim.payload.get("ref_publication")
    if isinstance(publication, dict) and publication.get("phase") != "removed":
        return True
    temporary_refs = claim.payload.get("temporary_refs")
    return isinstance(temporary_refs, list) and any(
        not isinstance(record, dict) or record.get("phase") != "removed"
        for record in temporary_refs
    )
