from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.core.workspace.naming import temporary_import_ref_prefix
from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.state_evidence import record_workspace_temporary_ref
from crewplane.runtime.workspace.worktree.ref_cleanup import delete_run_workspace_refs
from crewplane.runtime.workspace.worktree.temporary_refs import (
    TemporaryRefOwner,
    reconcile_temporary_import_refs,
)
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_plan,
)

isolated_git = isolated_git_support.isolated_git


@dataclass(frozen=True)
class CleanupEvidence:
    repo: Path
    run_dir: Path
    owner: TemporaryRefOwner
    ref_name: str
    oid: str
    repository_id: str
    run_key: str

    def cleanup(self) -> int:
        return delete_run_workspace_refs(
            self.repo, self.repo / ".git", self.repo, self.run_key, self.run_dir
        )

    def payload(self) -> dict[str, object]:
        return read_json_object(self.owner.state_path)

    def write(self, payload: dict[str, object]) -> None:
        self.owner.state_path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def evidence(tmp_path: Path, isolated_git: IsolatedGit) -> CleanupEvidence:
    del isolated_git
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", True, kind="worktree")
    source = plan.workspace_source
    assert source is not None
    run_dir = repo / ".crewplane" / "execution-stages" / plan.run_key_name
    owner = TemporaryRefOwner.dedicated(
        plan, source, run_dir / "logs", "implement", "consumer"
    )
    owner.prepare()
    slug = invocation_slug("implement", "consumer", None, 0)
    ref_name = (
        temporary_import_ref_prefix(plan.run_key_name, "implement", slug) + "source"
    )
    oid = run_git_text(repo, "rev-parse", "HEAD")
    record_workspace_temporary_ref(owner.state_path, ref_name, oid)
    run_git_text(repo, "update-ref", ref_name, oid)
    return CleanupEvidence(
        repo, run_dir, owner, ref_name, oid, source.repository_id, plan.run_key_name
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("run_key_name", "different-run", "contradictory run"),
        ("git", {}, "different repository"),
        ("temporary_refs", "invalid", "evidence is invalid"),
        ("round_num", "zero", "evidence is invalid"),
        ("run_id", None, "evidence is invalid"),
        ("task_id", 7, "evidence is invalid"),
    ],
)
def test_ref_cleanup_preserves_refs_when_evidence_identity_is_invalid(
    evidence: CleanupEvidence, field: str, value: object, message: str
) -> None:
    payload = evidence.payload()
    payload[field] = value
    evidence.write(payload)
    with pytest.raises(RuntimeError, match=message):
        evidence.cleanup()
    assert run_git_text(evidence.repo, "rev-parse", evidence.ref_name) == evidence.oid
    assert evidence.owner.state_path.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_id", "other"),
        ("phase", "unknown"),
        ("owner_run_id", "other"),
        ("owner_node_id", "other"),
        ("owner_task_id", "other"),
        ("owner_role", "other"),
        ("owner_round_num", 2),
        ("owner_audit_round_num", 3),
        ("name", None),
        ("target_oid", "not-an-oid"),
    ],
)
def test_ref_cleanup_validates_each_durable_ownership_claim(
    evidence: CleanupEvidence, field: str, value: object
) -> None:
    payload = evidence.payload()
    claims = payload["temporary_refs"]
    assert isinstance(claims, list)
    claims[0][field] = value
    evidence.write(payload)
    original_evidence = evidence.owner.state_path.read_bytes()
    with pytest.raises(
        RuntimeError, match="different repository|evidence is contradictory"
    ):
        evidence.cleanup()
    assert run_git_text(evidence.repo, "rev-parse", evidence.ref_name) == evidence.oid
    assert evidence.owner.state_path.exists()
    assert evidence.owner.state_path.read_bytes() == original_evidence


def test_dedicated_cleanup_accepts_writer_produced_claim(evidence: CleanupEvidence):
    assert evidence.payload()["temporary_refs"] == [
        {
            "phase": "prepared",
            "name": evidence.ref_name,
            "target_oid": evidence.oid,
            "owner_run_id": evidence.payload()["run_id"],
            "owner_node_id": "implement",
            "owner_task_id": "consumer",
            "owner_role": "artifact_consumer",
            "owner_round_num": 0,
            "owner_audit_round_num": None,
            "repository_id": evidence.repository_id,
        }
    ]
    assert evidence.cleanup() == 1
    assert not evidence.owner.state_path.exists()
    with pytest.raises(subprocess.CalledProcessError):
        run_git_text(evidence.repo, "rev-parse", "--verify", evidence.ref_name)


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize(
    ("damage", "failure_kind"),
    [
        ("missing", "container"),
        ("null", "container"),
        ("malformed", "container"),
        ("non-dict-claim", "claim"),
        ("foreign-envelope", "envelope"),
        ("non-dict-envelope", "envelope"),
        ("foreign-claim", "claim"),
        ("foreign-second-claim", "claim"),
        ("foreign-envelope-and-claim", "envelope"),
    ],
)
def test_repository_identity_rejection_preserves_all_refs_and_evidence(
    evidence, external, damage, failure_kind
) -> None:
    second_ref = evidence.ref_name + "-second"
    record_workspace_temporary_ref(evidence.owner.state_path, second_ref, evidence.oid)
    run_git_text(evidence.repo, "update-ref", second_ref, evidence.oid)
    payload = evidence.payload()
    if damage == "missing":
        del payload["temporary_refs"]
    elif damage == "null":
        payload["temporary_refs"] = None
    elif damage == "malformed":
        payload["temporary_refs"] = False
    elif damage == "non-dict-claim":
        payload["temporary_refs"][1] = None
    elif damage == "non-dict-envelope":
        payload["git"] = []
    else:
        if "envelope" in damage:
            payload["git"]["repo_id"] = "foreign"
        if "claim" in damage:
            index = 1 if "second" in damage else 0
            payload["temporary_refs"][index]["repository_id"] = "foreign"
    evidence.write(payload)
    original = evidence.owner.state_path.read_bytes()

    def cleanup():
        if external:
            return evidence.cleanup()
        return reconcile_temporary_import_refs(
            evidence.owner.state_path,
            evidence.repo,
            evidence.repo / ".git",
            evidence.repository_id,
        )

    if not external and damage in {"missing", "null"}:
        assert cleanup() == 0
    else:
        if external:
            messages = {
                "container": "Workspace temporary ref cleanup evidence is invalid",
                "envelope": "Workspace ref cleanup evidence belongs to a different repository",
                "claim": "Workspace ref cleanup claim belongs to a different repository",
            }
            expected = f"{messages[failure_kind]}: {evidence.owner.state_path}."
        else:
            messages = {
                "container": "Workspace temporary ref cleanup evidence is invalid.",
                "envelope": "Workspace temporary ref cleanup repository identity changed.",
                "claim": "Workspace temporary ref cleanup claim repository identity changed.",
            }
            expected = messages[failure_kind]
        with pytest.raises(RuntimeError) as caught:
            cleanup()
        assert str(caught.value) == expected
        assert caught.value.__cause__ is None
    assert evidence.owner.state_path.read_bytes() == original
    for name in (evidence.ref_name, second_ref):
        assert run_git_text(evidence.repo, "rev-parse", name) == evidence.oid


@pytest.mark.parametrize("kind", ["malformed-json", "directory", "symlink"])
def test_ref_cleanup_refuses_unreadable_or_unsafe_claim_file(
    evidence: CleanupEvidence, kind: str
) -> None:
    path = evidence.owner.state_path
    path.unlink()
    if kind == "malformed-json":
        path.write_text("{invalid", encoding="utf-8")
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(evidence.repo / "README.md")
    with pytest.raises(RuntimeError, match="malformed evidence|unsafe evidence"):
        evidence.cleanup()
    assert run_git_text(evidence.repo, "rev-parse", evidence.ref_name) == evidence.oid


@pytest.mark.parametrize(
    "plan_payload",
    [
        None,
        [],
        {},
        {"run_key_name": "different", "nodes": []},
        {"run_key_name": "workspace-run-001", "nodes": None},
        {"run_key_name": "workspace-run-001", "nodes": [None]},
        {
            "run_key_name": "workspace-run-001",
            "nodes": [{"artifact_contract": {"stage_path": "../outside"}}],
        },
    ],
)
def test_ref_cleanup_refuses_invalid_planned_stage_evidence(
    evidence: CleanupEvidence, plan_payload: object
) -> None:
    plan_file = evidence.run_dir / "preflight" / "execution-plan.json"
    plan_file.parent.mkdir()
    plan_file.write_text(json.dumps(plan_payload), encoding="utf-8")
    with pytest.raises(
        RuntimeError, match="contradictory plan evidence|unsafe stage evidence"
    ):
        evidence.cleanup()
    assert run_git_text(evidence.repo, "rev-parse", evidence.ref_name) == evidence.oid


@pytest.mark.parametrize(
    "kind", ["invalid-json", "invalid-utf8", "hardlink", "symlink"]
)
def test_ref_cleanup_refuses_unsafe_execution_plan(
    evidence: CleanupEvidence, kind: str
) -> None:
    plan_file = evidence.run_dir / "preflight" / "execution-plan.json"
    plan_file.parent.mkdir()
    if kind == "invalid-json":
        plan_file.write_bytes(b"{broken")
    elif kind == "invalid-utf8":
        plan_file.write_bytes(b"\xff")
    elif kind == "symlink":
        plan_file.symlink_to(evidence.repo / "README.md")
    else:
        plan_file.hardlink_to(evidence.repo / "README.md")
    with pytest.raises(
        RuntimeError, match="malformed plan evidence|unsafe plan evidence"
    ):
        evidence.cleanup()
    assert run_git_text(evidence.repo, "rev-parse", evidence.ref_name) == evidence.oid


@pytest.mark.parametrize(
    "kind", ["stage-symlink", "logs-symlink", "logs-file", "nested-stage-file"]
)
def test_ref_cleanup_refuses_unowned_evidence_directories(
    evidence: CleanupEvidence, kind: str
) -> None:
    if kind == "stage-symlink":
        (evidence.run_dir / "stage").symlink_to(evidence.repo, target_is_directory=True)
    elif kind.startswith("logs"):
        logs = evidence.run_dir / "logs"
        logs.rename(evidence.run_dir / "saved")
        if kind == "logs-symlink":
            logs.symlink_to(evidence.run_dir / "saved", target_is_directory=True)
        else:
            logs.write_bytes(b"keep")
    else:
        (evidence.run_dir / "parent").mkdir()
        (evidence.run_dir / "parent" / "child").write_bytes(b"keep")
        plan_file = evidence.run_dir / "preflight" / "execution-plan.json"
        plan_file.parent.mkdir()
        plan_file.write_text(
            json.dumps(
                {
                    "run_key_name": evidence.run_key,
                    "nodes": [{"artifact_contract": {"stage_path": "parent/child"}}],
                }
            ),
            encoding="utf-8",
        )
    with pytest.raises(RuntimeError, match="unsafe evidence|unsafe stage evidence"):
        evidence.cleanup()
    assert run_git_text(evidence.repo, "rev-parse", evidence.ref_name) == evidence.oid


@pytest.mark.parametrize("change", ["missing", "moved", "symbolic"])
def test_temporary_ref_cleanup_respects_current_git_identity(
    evidence: CleanupEvidence, change: str
) -> None:
    run_git_text(evidence.repo, "update-ref", "-d", evidence.ref_name)
    if change == "missing":
        assert evidence.cleanup() == 0
        assert not evidence.owner.state_path.exists()
        return
    if change == "moved":
        run_git_text(evidence.repo, "commit", "--allow-empty", "-m", "later")
        current = run_git_text(evidence.repo, "rev-parse", "HEAD")
        run_git_text(evidence.repo, "update-ref", evidence.ref_name, current)
    else:
        current = evidence.oid
        run_git_text(evidence.repo, "symbolic-ref", evidence.ref_name, "HEAD")
    with pytest.raises(
        RuntimeError, match="moved and was retained|symbolic and was retained"
    ):
        evidence.cleanup()
    assert run_git_text(evidence.repo, "rev-parse", evidence.ref_name) == current
    assert evidence.owner.state_path.exists()


def test_temporary_cleanup_rejects_dangling_symbolic_ref(evidence: CleanupEvidence):
    target = "refs/heads/missing-target"
    run_git_text(evidence.repo, "update-ref", "-d", evidence.ref_name)
    run_git_text(evidence.repo, "symbolic-ref", evidence.ref_name, target)
    original_evidence = evidence.owner.state_path.read_bytes()
    with pytest.raises(RuntimeError) as caught:
        evidence.cleanup()
    assert str(caught.value) == (
        "Workspace temporary import ref is symbolic and was retained: "
        f"{evidence.ref_name} -> {target}."
    )
    assert run_git_text(evidence.repo, "symbolic-ref", evidence.ref_name) == target
    with pytest.raises(subprocess.CalledProcessError):
        run_git_text(evidence.repo, "rev-parse", "--verify", target)
    assert evidence.owner.state_path.read_bytes() == original_evidence


@pytest.mark.parametrize("failed_command", ["symbolic-ref", "rev-parse"])
def test_temporary_lookup_error_preserves_refs_and_evidence(
    evidence: CleanupEvidence, monkeypatch: pytest.MonkeyPatch, failed_command: str
):
    original_text = GitCommand.text
    failure = subprocess.CalledProcessError(42, ["git", failed_command])

    def failing_text(command, *args):
        if args[0] == failed_command and args[-1] == evidence.ref_name:
            raise failure
        return original_text(command, *args)

    original_evidence = evidence.owner.state_path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(GitCommand, "text", failing_text)
        with pytest.raises(subprocess.CalledProcessError) as caught:
            evidence.cleanup()
    assert caught.value is failure
    assert run_git_text(evidence.repo, "rev-parse", evidence.ref_name) == evidence.oid
    assert evidence.owner.state_path.read_bytes() == original_evidence


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("temporary_refs", False, "evidence is invalid"),
        ("git", {}, "repository identity changed"),
        ("task_id", None, "invocation identity"),
        ("run_key_name", "", "lacks run_key_name"),
    ],
)
def test_temporary_reconciliation_rejects_corrupt_input(
    evidence: CleanupEvidence, field: str, value: object, message: str
) -> None:
    payload = evidence.payload()
    payload[field] = value
    evidence.write(payload)
    with pytest.raises(RuntimeError, match=message):
        reconcile_temporary_import_refs(
            evidence.owner.state_path,
            evidence.repo,
            evidence.repo / ".git",
            evidence.repository_id,
        )
    assert run_git_text(evidence.repo, "rev-parse", evidence.ref_name) == evidence.oid


def test_temporary_ref_owner_retains_unresolved_claims_and_discards_removed_evidence(
    evidence: CleanupEvidence,
) -> None:
    evidence.owner.discard_resolved_evidence()
    assert evidence.owner.state_path.exists()
    assert (
        reconcile_temporary_import_refs(
            evidence.owner.state_path,
            evidence.repo,
            evidence.repo / ".git",
            evidence.repository_id,
        )
        == 1
    )
    evidence.owner.discard_resolved_evidence()
    assert not evidence.owner.state_path.exists()
