from __future__ import annotations

import pytest

from crewplane.core.workspace.git_policy import GitRefSyntaxIssue, git_ref_syntax_issue
from crewplane.core.workspace.policy import validate_branch_name


@pytest.mark.parametrize("char", [*(chr(i) for i in range(32)), chr(127), *" ~^:?*[\\"])
def test_branch_and_result_refs_reject_disallowed_characters(char: str) -> None:
    name = f"feature/a{char}b"
    with pytest.raises(ValueError) as error:
        validate_branch_name(name)
    assert str(error.value) == "branch_name contains characters Git refs do not allow"
    assert (
        git_ref_syntax_issue(f"refs/crewplane/{name}/result")
        == GitRefSyntaxIssue.CHARACTERS
    )


@pytest.mark.parametrize(
    "name",
    [
        "a..b",
        "a//b",
        "a@{b",
        ".hidden",
        "a/.hidden",
        "a.lock",
        "a.lock/b",
        "a./b",
        "a/",
        "a.",
    ],
)
def test_branch_and_result_refs_reject_invalid_components(name: str) -> None:
    with pytest.raises(ValueError) as error:
        validate_branch_name(name)
    assert str(error.value) == "branch_name is not a valid Git branch name"
    assert git_ref_syntax_issue(f"refs/crewplane/{name}/result") is not None


@pytest.mark.parametrize(
    "name",
    [
        "feature/日本語",
        "Ünicode",
        "a\u0080b",
        "a@b",
        "a.LOCK",
        "a.locked",
        "a-b_c.d",
        "refs-in-name",
    ],
)
def test_branch_and_result_refs_accept_valid_components(name: str) -> None:
    assert validate_branch_name(name) == name
    assert git_ref_syntax_issue(f"refs/crewplane/{name}/result") is None


@pytest.mark.parametrize(
    ("name", "message"),
    [
        (" a?..", "branch_name cannot be blank or padded with whitespace"),
        ("refs/a?..", "branch_name must be a branch name, not a ref path"),
        ("-a?..", "branch_name must be a branch name, not a ref path"),
        ("a?..", "branch_name is not a valid Git branch name"),
        ("a?//b", "branch_name is not a valid Git branch name"),
        ("a?@{b", "branch_name is not a valid Git branch name"),
        ("a?/", "branch_name is not a valid Git branch name"),
        ("a?/b.lock", "branch_name contains characters Git refs do not allow"),
        ("a?/.hidden", "branch_name contains characters Git refs do not allow"),
    ],
)
def test_branch_diagnostics_preserve_validation_precedence(name, message) -> None:
    with pytest.raises(ValueError) as error:
        validate_branch_name(name)
    assert str(error.value) == message
