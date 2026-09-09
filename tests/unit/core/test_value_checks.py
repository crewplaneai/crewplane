from __future__ import annotations

import pytest

from crewplane.core.value_checks import is_sha256
from crewplane.core.workspace.git_policy import is_git_object_id


@pytest.mark.parametrize(
    ("value", "git_object", "sha256"),
    [
        ("a" * 40, True, False),
        ("0123456789abcdef" * 4, True, True),
        ("0" * 64, True, True),
        ("a" * 39, False, False),
        ("a" * 41, False, False),
        ("a" * 63, False, False),
        ("a" * 65, False, False),
        ("A" * 40, False, False),
        ("A" * 64, False, False),
        ("g" * 64, False, False),
        ("a" * 63 + "\n", False, False),
        ("a" * 64 + "\n", False, False),
        (" " + "a" * 63, False, False),
        ("", False, False),
        (None, False, False),
        (True, False, False),
        (64, False, False),
        (b"a" * 64, False, False),
        (["a" * 64], False, False),
        ({"value": "a" * 64}, False, False),
    ],
)
def test_git_object_and_sha256_validation(
    value: object, git_object: bool, sha256: bool
) -> None:
    assert is_git_object_id(value) is git_object
    assert is_sha256(value) is sha256
