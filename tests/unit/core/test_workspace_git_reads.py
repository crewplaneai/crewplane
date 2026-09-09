import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.core.workspace import git_reads
from tests.helpers.workspace_preflight import init_git_repo


def test_literal_git_reads_preserve_special_paths_and_exact_bytes(
    tmp_path: Path,
) -> None:
    filename = "[source]*?.txt"
    content = b"exact\r\nblob\xff\x00"
    (tmp_path / filename).write_bytes(content)
    (tmp_path / "sibling.txt").write_text("sibling", encoding="utf-8")
    source = init_git_repo(tmp_path)

    record = git_reads.git_ls_tree(str(tmp_path), source.run_base_commit, filename)

    assert record is not None
    assert record.path == filename
    assert record.mode == "100644"
    assert record.object_type == "blob"
    assert git_reads.git_cat_blob(str(tmp_path), record.object_id) == content
    assert git_reads.git_ls_tree(str(tmp_path), source.run_base_commit, "*.txt") is None
    assert (
        git_reads.git_ls_tree(str(tmp_path), source.run_base_commit, "missing") is None
    )


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        (b"first\0second\0", "multiple tree entries"),
        (b"missing separator\0", "invalid tree entry"),
        (b"100644 blob\tpath\0", "invalid tree header"),
        (b"100644 blob extra oid\tpath\0", "invalid tree header"),
    ],
)
def test_literal_tree_read_rejects_ambiguous_or_malformed_records(
    stdout: bytes, message: str
) -> None:
    with (
        patch.object(
            git_reads,
            "run_git_literal",
            return_value=subprocess.CompletedProcess([], 0, stdout, b""),
        ),
        pytest.raises(ValueError, match=message),
    ):
        git_reads.git_ls_tree("/repo", "commit", "path")


def test_literal_tree_read_preserves_surrogate_escaped_filenames() -> None:
    stdout = b"100644 blob abc\tname\xff.txt\0"
    with patch.object(
        git_reads,
        "run_git_literal",
        return_value=subprocess.CompletedProcess([], 0, stdout, b""),
    ):
        record = git_reads.git_ls_tree("/repo", "commit", "name\udcff.txt")

    assert record == git_reads.GitTreeRecord("100644", "blob", "abc", "name\udcff.txt")


def test_literal_tree_read_requires_ascii_metadata() -> None:
    with (
        patch.object(
            git_reads,
            "run_git_literal",
            return_value=subprocess.CompletedProcess(
                [], 0, b"100644 bl\xffb abc\tpath\0", b""
            ),
        ),
        pytest.raises(UnicodeDecodeError),
    ):
        git_reads.git_ls_tree("/repo", "commit", "path")


@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["git"], stderr=b"read failed"),
        subprocess.TimeoutExpired(["git"], 30.0),
    ],
)
def test_git_reads_preserve_subprocess_failure_and_bounded_sanitized_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, literal: bool, error: Exception
) -> None:
    monkeypatch.setenv("GIT_DIR", "/untrusted")
    run = git_reads.run_git_literal if literal else git_reads.run_git
    with (
        patch.object(git_reads.subprocess, "run", side_effect=error) as process,
        pytest.raises(type(error)) as caught,
    ):
        run(tmp_path, "ls-tree", "HEAD")

    assert caught.value is error
    command = process.call_args.args[0]
    options = process.call_args.kwargs
    assert ("--literal-pathspecs" in command) is literal
    assert command[-4:] == ["-C", str(tmp_path), "ls-tree", "HEAD"]
    assert options["timeout"] == 30.0
    assert options["check"] is True
    assert options["capture_output"] is True
    assert "GIT_DIR" not in options["env"]
    assert options["env"]["GIT_OPTIONAL_LOCKS"] == "0"
