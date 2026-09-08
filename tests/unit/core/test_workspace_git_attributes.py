from __future__ import annotations

import pytest

from crewplane.core.workspace.git_attributes import (
    attribute_records,
    byte_transforming_attribute,
)


@pytest.mark.parametrize(
    "attribute", ["crlf", "eol", "filter", "ident", "text", "working-tree-encoding"]
)
@pytest.mark.parametrize(
    ("value", "rejected"),
    [
        ("set", True),
        ("auto", True),
        ("lfs", True),
        ("unset", False),
        ("unspecified", False),
    ],
)
def test_byte_transforming_attribute_policy(
    attribute: str, value: str, rejected: bool
) -> None:
    assert byte_transforming_attribute(attribute, value) is rejected


@pytest.mark.parametrize("attribute", ["diff", "export-ignore", "custom"])
def test_harmless_attributes_are_allowed(attribute: str) -> None:
    assert not byte_transforming_attribute(attribute, "set")


def test_attribute_records_normalize_names_and_preserve_paths_and_values() -> None:
    assert attribute_records(
        ("docs/Café\tname.md", "FILTER", "MyFilter", "other\nfile", "Text", "Unset")
    ) == (
        ("docs/Café\tname.md", "filter", "MyFilter"),
        ("other\nfile", "text", "Unset"),
    )
    assert attribute_records(()) == ()


@pytest.mark.parametrize("records", [("path",), ("path", "text")])
def test_attribute_records_reject_incomplete_triples(records: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        attribute_records(records)
