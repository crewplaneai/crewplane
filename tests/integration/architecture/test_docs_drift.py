from __future__ import annotations

from tests.integration.architecture.static_checks import (
    REPO_ROOT,
    SRC_ROOT,
    ForbiddenTextRule,
    find_forbidden_text,
    python_files,
)

LEGACY_PROMPT_CONFIG_RULE = ForbiddenTextRule(
    name="public docs and templates omit legacy prompt config",
    paths=(
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "architecture" / "modular-orchestration-architecture.md",
        SRC_ROOT / "crewplane" / "example_templates" / "config.yml",
    ),
    forbidden_terms=frozenset(
        {
            "prompt_arg",
            "quota_parser",
            "stdin_prompt_arg",
            "use_stdin",
        }
    ),
)

STALE_VERSION_IMPORT_RULE = ForbiddenTextRule(
    name="production source uses the canonical version catalog",
    paths=python_files(SRC_ROOT / "crewplane"),
    forbidden_terms=frozenset(
        {
            "crewplane.architecture.api_version",
            "crewplane.core.versions",
            "crewplane.versions",
        }
    ),
)


def test_docs_and_templates_do_not_reference_legacy_prompt_config_fields() -> None:
    assert find_forbidden_text(LEGACY_PROMPT_CONFIG_RULE) == []


def test_version_catalog_has_single_public_python_source() -> None:
    stale_paths = [
        SRC_ROOT / "crewplane" / "versions.py",
        SRC_ROOT / "crewplane" / "core" / "versions.py",
        SRC_ROOT / "crewplane" / "architecture" / "api_version.py",
    ]
    assert [
        path.relative_to(REPO_ROOT).as_posix() for path in stale_paths if path.exists()
    ] == []

    assert find_forbidden_text(STALE_VERSION_IMPORT_RULE) == []
