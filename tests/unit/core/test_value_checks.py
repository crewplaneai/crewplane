from __future__ import annotations

import pytest

from crewplane.core.value_checks import is_sha256, optional_strict_int
from crewplane.core.workspace.git_policy import is_git_object_id


class IntegerSubclass(int):
    pass


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        (0, True),
        (-1, True),
        (1, True),
        (2**80, True),
        pytest.param(IntegerSubclass(7), True, id="integer-subclass"),
        (True, False),
        (False, False),
        (None, False),
        pytest.param("1", False, id="numeric-string"),
        (1.0, False),
        (1.5, False),
        ([], False),
        ({}, False),
        (object(), False),
    ],
)
def test_optional_strict_int_preserves_identity(value: object, accepted: bool) -> None:
    result = optional_strict_int(value)
    if accepted:
        assert result is value
    else:
        assert result is None


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
