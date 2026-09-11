from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.artifacts.generated_files.catalog import (
    build_generated_file_links_section,
    generated_file_links_for_content,
)
from crewplane.artifacts.generated_files.paths import (
    generated_file_path_belongs_to_node,
)


@pytest.mark.parametrize(
    ("namespace", "relative", "label"),
    [
        (None, "generated-files/build.node/src/app.txt", "src/app.txt"),
        ("alpha", "generated-files/build.node/alpha/src/app.txt", "alpha/src/app.txt"),
        (
            "./alpha beta/",
            "generated-files/build.node/alpha-beta/src/app.txt",
            "./alpha beta//src/app.txt",
        ),
    ],
)
def test_publication_preserves_namespace_paths_bytes_and_links(
    tmp_path: Path, namespace: str | None, relative: str, label: str
) -> None:
    workspace = tmp_path / "workspace"
    generated = workspace / "src" / "app.txt"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"exact published bytes\x00\xff")
    output = tmp_path / "provider.md"
    output.write_text("Updated `src/app.txt`.\n")
    result_path = tmp_path / "results" / "build-result.md"

    result = generated_file_links_for_content(
        output.read_text(),
        workspace,
        result_path,
        "build.node",
        materialize=True,
        copy_namespace=namespace,
        candidate_files=(generated,),
    )

    target = result_path.parent / relative
    assert tuple(link.target_path for link in result.links) == (target,)
    assert target.read_bytes() == generated.read_bytes()
    assert result.warnings == ()
    assert generated_file_path_belongs_to_node(relative, "build.node")
    assert build_generated_file_links_section(result_path, result.links) == (
        f"## Generated Files\n\n- [{label}]({relative})\n"
    )


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("generated-files/build/alpha/app.txt", True),
        ("generated-files/build/src/app.txt", True),
        ("generated-files/build/app.txt", False),
        ("generated-files/other/alpha/app.txt", False),
        ("elsewhere/build/alpha/app.txt", False),
        ("/generated-files/build/alpha/app.txt", False),
        ("generated-files/build//app.txt", False),
        ("generated-files/build/./app.txt", False),
        ("generated-files/build/../app.txt", False),
        ("generated-files/build/alpha/app.txt/", False),
        ("generated-files/build/alpha/./app.txt", False),
        ("generated-files/build/alpha/../app.txt", False),
        ("generated-files/build/alpha//app.txt", False),
    ],
)
def test_generated_path_ownership_validates_raw_components(relative, expected):
    assert generated_file_path_belongs_to_node(relative, "build") is expected
