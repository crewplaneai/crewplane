from __future__ import annotations

import pytest

from tests.integration.architecture.static_checks import (
    SRC_ROOT,
    TESTS_ROOT,
    PrivateApiRule,
    find_private_attribute_access,
    find_private_imports,
    find_private_patch_targets,
)

REPO_PRIVATE_API_RULE = PrivateApiRule(
    name="repo modules expose cross-module collaborators publicly",
    roots=(SRC_ROOT, TESTS_ROOT),
)


@pytest.mark.parametrize(
    ("check", "rule"),
    (
        pytest.param(
            find_private_imports,
            REPO_PRIVATE_API_RULE,
            id="private-imports",
        ),
        pytest.param(
            find_private_attribute_access,
            REPO_PRIVATE_API_RULE,
            id="private-attribute-access",
        ),
        pytest.param(
            find_private_patch_targets,
            REPO_PRIVATE_API_RULE,
            id="private-patch-targets",
        ),
    ),
)
def test_private_api_rule(check, rule: PrivateApiRule) -> None:
    assert check(rule) == []
