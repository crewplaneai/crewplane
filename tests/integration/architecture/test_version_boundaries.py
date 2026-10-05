from tests.integration.architecture.static_checks import (
    SRC_ROOT,
    ForbiddenImportRule,
    find_forbidden_imports,
)


def test_version_catalog_has_single_public_python_source() -> None:
    obsolete_modules = (
        "crewplane.architecture.api_version",
        "crewplane.core.versions",
        "crewplane.versions",
    )
    obsolete_paths = [
        SRC_ROOT.joinpath(*module.split(".")).with_suffix(".py")
        for module in obsolete_modules
    ]
    assert [path for path in obsolete_paths if path.exists()] == []
    rule = ForbiddenImportRule(
        name="production imports the canonical version catalog",
        roots=(SRC_ROOT / "crewplane",),
        forbidden_prefixes=obsolete_modules,
    )
    assert find_forbidden_imports(rule) == []
