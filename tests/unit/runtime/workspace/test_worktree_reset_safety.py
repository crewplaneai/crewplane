import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from crewplane.runtime.workspace.worktree import reset


class GitCommand:
    def __init__(self, head: str = "expected", branch: str = "") -> None:
        self.head = head
        self.branch = branch
        self.runs: list[tuple[str, ...]] = []

    def run(self, *args: str) -> None:
        self.runs.append(args)

    def text(self, *args: str) -> str:
        return self.head if args[0] == "rev-parse" else self.branch


def worktree_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    checkout = tmp_path / "checkout"
    common_git_dir = tmp_path / "repo" / ".git"
    git_dir = common_git_dir / "worktrees" / "checkout"
    checkout.mkdir()
    git_dir.mkdir(parents=True)
    (checkout / ".git").write_text(
        f"gitdir: {git_dir.as_posix()}\n",
        encoding="utf-8",
    )
    (git_dir / "gitdir").write_text(
        (checkout / ".git").as_posix(),
        encoding="utf-8",
    )
    return checkout, common_git_dir, git_dir


def run_reset(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    common_git_dir: Path,
    git_dir: Path,
    command: GitCommand | None = None,
    changed_paths: tuple[str, ...] = (),
) -> GitCommand:
    selected_command = command or GitCommand()
    monkeypatch.setattr(reset, "git", Mock(return_value=selected_command))
    monkeypatch.setattr(
        reset,
        "reject_common_git_policy_drift",
        Mock(return_value=None),
    )
    monkeypatch.setattr(
        reset,
        "reject_worktree_git_policy_drift",
        Mock(return_value=None),
    )
    monkeypatch.setattr(reset, "changed_paths", Mock(return_value=changed_paths))
    reset.reset_reusable_worktree_checkout(
        checkout,
        "expected",
        common_git_dir.parent,
        common_git_dir,
        git_dir,
    )
    return selected_command


def test_reset_removes_policy_files_and_runs_full_git_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)
    info_dir = git_dir / "info"
    info_dir.mkdir()
    for path in (
        git_dir / "config.worktree",
        info_dir / "attributes",
        info_dir / "exclude",
    ):
        path.write_text("policy", encoding="utf-8")

    command = run_reset(monkeypatch, checkout, common_git_dir, git_dir)

    assert list(info_dir.iterdir()) == []
    assert not (git_dir / "config.worktree").exists()
    assert command.runs == [
        ("reset", "--hard", "expected"),
        ("checkout", "--detach", "expected"),
        ("clean", "-dffx"),
    ]


def test_reset_allows_missing_optional_info_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)

    run_reset(monkeypatch, checkout, common_git_dir, git_dir)


def test_reset_rejects_non_directory_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)
    (git_dir / "info").write_text("not a directory", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Git info dir must be a real directory"):
        run_reset(monkeypatch, checkout, common_git_dir, git_dir)


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("directory", id="directory"),
        pytest.param("symlink", id="symlink"),
    ],
)
def test_reset_rejects_unsafe_policy_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)
    policy_file = git_dir / "config.worktree"
    if kind == "directory":
        policy_file.mkdir()
    else:
        target = tmp_path / "target"
        target.write_text("outside", encoding="utf-8")
        try:
            policy_file.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match="policy file is not a real file"):
        run_reset(monkeypatch, checkout, common_git_dir, git_dir)


def test_reset_resolves_relative_git_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)
    relative_git_dir = git_dir.relative_to(checkout, walk_up=True)
    (checkout / ".git").write_text(
        f"gitdir: {relative_git_dir.as_posix()}\n",
        encoding="utf-8",
    )

    run_reset(monkeypatch, checkout, common_git_dir, git_dir)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        pytest.param("missing", "requires a valid worktree .git file", id="missing"),
        pytest.param(
            "directory", "requires a valid worktree .git file", id="directory"
        ),
        pytest.param("invalid", "invalid worktree .git file", id="invalid-marker"),
        pytest.param("empty", "empty worktree Git dir", id="empty-path"),
        pytest.param("escape", "escapes the common Git dir", id="escape"),
    ],
)
def test_reset_rejects_invalid_git_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    message: str,
) -> None:
    checkout = tmp_path / "checkout"
    common_git_dir = tmp_path / "repo" / ".git"
    expected_git_dir = common_git_dir / "worktrees" / "checkout"
    checkout.mkdir()
    common_git_dir.mkdir(parents=True)
    git_file = checkout / ".git"
    if setup == "directory":
        git_file.mkdir()
    elif setup == "invalid":
        git_file.write_text("not a marker", encoding="utf-8")
    elif setup == "empty":
        git_file.write_text("gitdir:  ", encoding="utf-8")
    elif setup == "escape":
        git_file.write_text(f"gitdir: {(tmp_path / 'outside').as_posix()}")

    with pytest.raises(RuntimeError, match=message):
        run_reset(
            monkeypatch,
            checkout,
            common_git_dir,
            expected_git_dir,
        )


def test_reset_checks_expected_git_dir_and_backlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)
    with pytest.raises(RuntimeError, match="does not match Git dir"):
        run_reset(
            monkeypatch,
            checkout,
            common_git_dir,
            common_git_dir / "worktrees" / "other",
        )

    (git_dir / "gitdir").write_text("other-checkout/.git", encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not belong to checkout"):
        run_reset(monkeypatch, checkout, common_git_dir, git_dir)


def test_reset_rejects_missing_or_symlinked_git_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)
    (checkout / ".git").write_text(
        f"gitdir: {(common_git_dir / 'worktrees' / 'missing').as_posix()}",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="metadata is missing"):
        run_reset(
            monkeypatch,
            checkout,
            common_git_dir,
            common_git_dir / "worktrees" / "missing",
        )

    target = common_git_dir / "real-admin"
    target.mkdir()
    (target / "gitdir").write_text((checkout / ".git").as_posix())
    linked = common_git_dir / "worktrees" / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    (checkout / ".git").write_text(f"gitdir: {linked.as_posix()}")
    with pytest.raises(RuntimeError, match="must not be symlinked"):
        run_reset(monkeypatch, checkout, common_git_dir, target)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        pytest.param("missing", "missing its checkout pointer", id="missing"),
        pytest.param("directory", "checkout pointer is invalid", id="directory"),
        pytest.param("empty", "checkout pointer is empty", id="empty"),
    ],
)
def test_reset_rejects_invalid_gitdir_backlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    message: str,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)
    backlink = git_dir / "gitdir"
    backlink.unlink()
    if setup == "directory":
        backlink.mkdir()
    elif setup == "empty":
        backlink.write_text(" \n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        run_reset(monkeypatch, checkout, common_git_dir, git_dir)


def test_reset_resolves_relative_gitdir_backlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)
    relative_backlink = (checkout / ".git").relative_to(git_dir, walk_up=True)
    (git_dir / "gitdir").write_text(relative_backlink.as_posix(), encoding="utf-8")

    run_reset(monkeypatch, checkout, common_git_dir, git_dir)


@pytest.mark.parametrize(
    ("command", "changed_paths", "message"),
    [
        pytest.param(
            GitCommand(head="wrong"),
            (),
            "source commit",
            id="commit",
        ),
        pytest.param(
            GitCommand(branch="main"),
            (),
            "detached HEAD",
            id="branch",
        ),
        pytest.param(
            GitCommand(),
            ("changed.txt",),
            "changed paths",
            id="changed-paths",
        ),
    ],
)
def test_reset_verifies_final_checkout_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: GitCommand,
    changed_paths: tuple[str, ...],
    message: str,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)

    with pytest.raises(RuntimeError, match=message):
        run_reset(
            monkeypatch,
            checkout,
            common_git_dir,
            git_dir,
            command=command,
            changed_paths=changed_paths,
        )


def test_reusable_reset_wraps_git_command_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, common_git_dir, git_dir = worktree_layout(tmp_path)

    def fail_policy(repo: Path, common: Path) -> None:
        del repo, common
        raise subprocess.CalledProcessError(1, ["git", "status"], stderr=b"failed")

    monkeypatch.setattr(reset, "reject_common_git_policy_drift", fail_policy)

    with pytest.raises(RuntimeError, match="Reusable workspace reset failed"):
        reset.reset_reusable_worktree_checkout(
            checkout,
            "expected",
            common_git_dir.parent,
            common_git_dir,
            git_dir,
        )
