from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.core.workspace.git_policy import has_replacement_refs


@pytest.mark.parametrize(
    "directory",
    [None, "", "refs/replace", "refs/replace/nested/deeper"],
    ids=["absent-root", "absent-refs", "empty-refs", "nested-directories"],
)
def test_replacement_refs_ignore_absent_paths_and_directories(
    tmp_path: Path, directory: str | None
) -> None:
    git_dir = tmp_path / "git"
    if directory is not None:
        (git_dir / directory).mkdir(parents=True)

    assert not has_replacement_refs(git_dir)


def test_replacement_refs_do_not_treat_the_loose_root_file_as_a_ref(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / "git"
    (git_dir / "refs").mkdir(parents=True)
    (git_dir / "refs" / "replace").write_text("not a directory", encoding="utf-8")

    assert not has_replacement_refs(git_dir)


def test_replacement_refs_find_nested_loose_files_without_reading_contents(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / "git"
    ref = git_dir / "refs" / "replace" / "nested" / "ref"
    ref.parent.mkdir(parents=True)
    ref.write_bytes(b"")

    assert has_replacement_refs(git_dir)


def test_loose_replacement_ref_short_circuits_later_files_and_packed_refs(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / "git"
    replace_root = git_dir / "refs" / "replace"
    replace_root.mkdir(parents=True)
    (git_dir / "packed-refs").mkdir()
    files = (replace_root / "first", replace_root / "unreadable")
    with (
        patch.object(Path, "rglob", return_value=iter(files)),
        patch.object(
            Path,
            "is_file",
            side_effect=[True, PermissionError("later ref is unreadable")],
        ),
    ):
        assert has_replacement_refs(git_dir)


@pytest.mark.parametrize(
    ("content", "unfiltered", "filtered"),
    [
        (b"", False, False),
        (b"\n\n", False, False),
        (b"abc refs/heads/main\n", False, False),
        (b"abc refs/replace/original\n", True, True),
        (b"# abc refs/replace/comment\n", True, False),
        (b"^abc refs/replace/peeled\n", True, False),
        (b" # abc refs/replace/indented-comment\n", True, True),
        (b"unvalidated text refs/replace/substring suffix\n", True, True),
        (b"abc\trefs/replace/no-space\n", False, False),
        (b"abc refs/replace\n", False, False),
        (b"abc refs/repl\xfface/original\n", True, True),
        (b"# abc refs/repl\xfface/comment\n", True, False),
        (b"^abc refs/repl\xfface/peeled\n", True, False),
    ],
)
def test_packed_replacement_refs_preserve_substrings_decoding_and_prefix_policy(
    tmp_path: Path, content: bytes, unfiltered: bool, filtered: bool
) -> None:
    (tmp_path / "packed-refs").write_bytes(content)

    assert has_replacement_refs(tmp_path) is unfiltered
    assert (
        has_replacement_refs(tmp_path, ignored_packed_prefixes=("#", "^")) is filtered
    )


@pytest.mark.parametrize("method", ["exists", "rglob", "is_file", "read_text"])
@pytest.mark.parametrize("ignored_prefixes", [(), ("#", "^")])
def test_replacement_ref_filesystem_errors_propagate(
    tmp_path: Path, method: str, ignored_prefixes: tuple[str, ...]
) -> None:
    ref = tmp_path / "refs" / "replace" / "nested" / "ref"
    ref.parent.mkdir(parents=True)
    if method == "is_file":
        ref.write_bytes(b"")
    (tmp_path / "packed-refs").write_bytes(b"abc refs/heads/main\n")
    failure = PermissionError("replacement ref inspection denied")

    with (
        patch.object(Path, method, side_effect=failure),
        pytest.raises(PermissionError) as caught,
    ):
        has_replacement_refs(tmp_path, ignored_prefixes)

    assert caught.value is failure


def test_packed_ref_existence_errors_propagate(tmp_path: Path) -> None:
    packed_refs = tmp_path / "packed-refs"
    real_exists = Path.exists
    failure = PermissionError("packed refs inspection denied")

    def exists(path: Path) -> bool:
        if path == packed_refs:
            raise failure
        return real_exists(path)

    with (
        patch.object(Path, "exists", new=exists),
        pytest.raises(PermissionError) as caught,
    ):
        has_replacement_refs(tmp_path)

    assert caught.value is failure
