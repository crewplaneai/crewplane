from __future__ import annotations

from typing import assert_type

from crewplane.core.value_checks import is_nonnegative_int, is_sha256
from crewplane.core.workspace.git_policy import is_git_object_id
from crewplane.runtime.execution.workspace_files.source_resolution import (
    WorkspaceCandidateSourceContext,
    is_initial_pre_review_context,
)


def check_source_context(
    value: WorkspaceCandidateSourceContext | None,
) -> None:
    if is_initial_pre_review_context(value):
        assert_type(value, WorkspaceCandidateSourceContext)
    else:
        assert_type(value, WorkspaceCandidateSourceContext | None)


def check_git_object_id(value: str | int) -> None:
    if is_git_object_id(value):
        assert_type(value, str)
    else:
        assert_type(value, str | int)


def check_nonnegative_int(value: int | str) -> None:
    if is_nonnegative_int(value):
        assert_type(value, int)
    else:
        assert_type(value, int | str)


def check_sha256(value: str | int) -> None:
    if is_sha256(value):
        assert_type(value, str)
    else:
        assert_type(value, str | int)
