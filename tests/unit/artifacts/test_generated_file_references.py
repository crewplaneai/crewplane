from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest
from markdown_it import MarkdownIt

from crewplane.artifacts import OutputManager
from crewplane.artifacts.generated_files.detection import GeneratedFileReferenceDetector
from tests.helpers.artifacts import node_artifact_request


@pytest.mark.parametrize(
    ("relative_path", "reference"),
    [
        pytest.param(
            "docs/review notes.md", "`docs/review notes.md`", id="code-spaces"
        ),
        pytest.param(
            "docs/review notes.md",
            "[Notes](<docs/review notes.md>)",
            id="angle-link-spaces",
        ),
        pytest.param(
            "docs/notes.md", '[Notes](docs/notes.md "Review notes")', id="link-title"
        ),
        pytest.param("docs/notes.md", "`docs/notes.md:12-20`", id="line-range"),
        pytest.param("docs/notes.md", "`'docs/notes.md'`", id="quoted-code"),
        pytest.param("docs/notes.md", "docs/notes.md", id="bare-path"),
    ],
)
def test_formatted_generated_file_reference_publishes_a_resolvable_link(
    tmp_path: Path,
    relative_path: str,
    reference: str,
) -> None:
    workspace = tmp_path / "workspace"
    generated_file = workspace / relative_path
    generated_file.parent.mkdir(parents=True)
    generated_file.write_text("Generated review notes.\n", encoding="utf-8")
    content = f"## Generated Files\n\n- {reference}\n"
    detector = GeneratedFileReferenceDetector(workspace)

    assert detector.detect(content) == (generated_file,)
    assert detector.detect_explicit_section(content) == (generated_file,)

    output = OutputManager("Workflow", base_dir=tmp_path / ".crewplane")
    request = node_artifact_request("build.node")
    stage_dir = output.create_node_dir(request)
    provider_output = stage_dir / "alpha_round1.md"
    provider_output.write_text(content, encoding="utf-8")

    finalized = output.finalize_node(
        request,
        generated_file_workspace_roots={provider_output: workspace},
    )

    assert len(finalized.generated_files) == 1
    published_file = finalized.generated_files[0]
    assert published_file.is_relative_to(output.results_dir / "generated-files")
    assert published_file.read_bytes() == generated_file.read_bytes()
    result_text = finalized.result_file.read_text(encoding="utf-8")
    links = [
        unquote(str(child.attrGet("href")))
        for token in MarkdownIt().parse(result_text)
        for child in token.children or ()
        if child.type == "link_open"
    ]
    assert any(
        (finalized.result_file.parent / link).resolve() == published_file
        for link in links
    )
    assert finalized.warnings == ()


@pytest.mark.parametrize(
    "next_heading",
    ["# References", "## References", "### References"],
    ids=["h1", "h2", "h3"],
)
def test_generated_file_section_ends_at_the_next_heading(
    tmp_path: Path,
    next_heading: str,
) -> None:
    generated_file = tmp_path / "created.md"
    reference_file = tmp_path / "reference.md"
    generated_file.write_text("new output", encoding="utf-8")
    reference_file.write_text("reference material", encoding="utf-8")
    content = (
        f"## Generated Files\n\n- `created.md`\n\n{next_heading}\n\n- `reference.md`\n"
    )
    detector = GeneratedFileReferenceDetector(tmp_path)

    assert detector.detect(content) == (generated_file,)
    assert detector.detect_explicit_section(content) == (generated_file,)
