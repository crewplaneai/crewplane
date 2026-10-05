import ast

import pytest

from tests.integration.architecture.static_checks import SRC_ROOT, imported_modules


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import crewplane.runtime", "crewplane.runtime"),
        ("import crewplane.runtime as rt", "crewplane.runtime"),
        ("from crewplane import runtime", "crewplane.runtime"),
        ("from crewplane import runtime as rt", "crewplane.runtime"),
        ("from ... import runtime", "crewplane.runtime"),
        ("from ...runtime import execution", "crewplane.runtime.execution"),
        ("from crewplane.runtime import *", "crewplane.runtime"),
    ],
)
def test_import_detection_covers_module_and_member_forms(
    source: str, module: str
) -> None:
    path = SRC_ROOT / "crewplane/architecture/ports/invoker.py"
    assert module in imported_modules(path, ast.parse(source).body[0])


def test_non_imports_do_not_create_dependencies() -> None:
    path = SRC_ROOT / "crewplane/core/example.py"
    assert (
        imported_modules(path, ast.parse("value = 'crewplane.runtime'").body[0]) == ()
    )
