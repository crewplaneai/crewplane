from __future__ import annotations

from pathlib import Path

from scripts.check_docs_links import broken_local_links, documentation_files


def test_documentation_files_include_maintainer_github_packaging_and_template_markdown(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "GOVERNANCE.md").write_text("# Governance\n", encoding="utf-8")
    (tmp_path / "MAINTAINERS.md").write_text("# Maintainers\n", encoding="utf-8")
    (tmp_path / "SUPPORT.md").write_text("# Support\n", encoding="utf-8")
    pull_request_template = tmp_path / ".github" / "pull_request_template.md"
    pull_request_template.parent.mkdir()
    pull_request_template.write_text("# Pull Request\n", encoding="utf-8")
    docs_file = tmp_path / "docs" / "guide.md"
    docs_file.parent.mkdir()
    docs_file.write_text("# Guide\n", encoding="utf-8")
    maintainer_file = tmp_path / "docs" / "maintainers" / "note.md"
    maintainer_file.parent.mkdir()
    maintainer_file.write_text("# Maintainer Note\n", encoding="utf-8")
    package_readme = tmp_path / "packaging" / "homebrew" / "README.md"
    package_readme.parent.mkdir(parents=True)
    package_readme.write_text("# Homebrew\n", encoding="utf-8")
    template_file = (
        tmp_path / "src" / "crewplane" / "example_templates" / "workspace.md"
    )
    template_file.parent.mkdir(parents=True)
    template_file.write_text("# Workspace\n", encoding="utf-8")

    assert documentation_files(tmp_path) == (
        pull_request_template,
        tmp_path / "GOVERNANCE.md",
        tmp_path / "MAINTAINERS.md",
        tmp_path / "README.md",
        tmp_path / "SUPPORT.md",
        docs_file,
        maintainer_file,
        package_readme,
        template_file,
    )


def test_broken_local_links_reports_only_missing_local_targets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "docs" / "index.md"
    source.parent.mkdir()
    target = tmp_path / "docs" / "guide.md"
    target.write_text("# Guide\n\n## Section\n", encoding="utf-8")
    escaped = tmp_path.parent / "outside.md"
    escaped.write_text("# Outside\n", encoding="utf-8")
    source.write_text(
        "\n".join(
            (
                "# Local",
                "",
                "[guide](guide.md#section)",
                "",
                "![image](missing.png)",
                "",
                "[missing](../missing.md)",
                "",
                "[external](https://example.com/missing.md)",
                "",
                "[anchor](#local)",
                "",
                "[stale anchor](guide.md#removed)",
                "",
                "[escaped](../../outside.md)",
            )
        ),
        encoding="utf-8",
    )

    assert broken_local_links((source,), repository_root=tmp_path) == (
        "docs/index.md:5: missing local link target 'missing.png'",
        "docs/index.md:7: missing local link target '../missing.md'",
        "docs/index.md:13: missing local link anchor '#removed' in docs/guide.md",
        "docs/index.md:15: local link target escapes the repository: "
        "'../../outside.md'",
    )


def test_markdown_anchor_validation_matches_duplicate_and_code_headings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "README.md"
    source.write_text(
        "\n".join(
            (
                "# `agents.<name>`",
                "# Repeat",
                "# Repeat",
                "",
                "[code](#agentsname)",
                "[first](#repeat)",
                "[second](#repeat-1)",
            )
        ),
        encoding="utf-8",
    )

    assert broken_local_links((source,), repository_root=tmp_path) == ()
