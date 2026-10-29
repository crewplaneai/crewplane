import math

import pytest

from crewplane.core.workflow.collections import (
    COLLECTION_BYTE_LIMIT,
    collection_bytes,
    decode_collection,
    validate_collection,
)


@pytest.mark.parametrize(
    "value",
    [
        [{1: "first", "1": "second"}],
        [{"x": math.inf}],
        [{"x": math.nan}],
        [{"x": {1, 2}}],
        [{"x": b"bytes"}],
        [1],
        {},
        [{"x": "\ud800"}],
    ],
)
def test_rejects_non_json_collections(value: object) -> None:
    with pytest.raises(ValueError, match="Foreach 'chunks'.*items"):
        validate_collection(value, 10, "chunks")


@pytest.mark.parametrize(
    "value",
    [
        b'[{"x":1,"x":2}]',
        b"[{}] extra",
        b"[NaN]",
        b'[{"x":1e400}]',
        b"\xff",
        b"```json\n[]\n```",
    ],
)
def test_strict_json_decoding(value: bytes) -> None:
    with pytest.raises(ValueError, match="Foreach 'chunks'"):
        decode_collection(value, 10, "chunks", "planner.findings")


def test_preserves_order_duplicates_and_template_text() -> None:
    items = [{"text": "{{file:outside}}"}, {"text": "{{file:outside}}"}]
    assert decode_collection(collection_bytes(items, 2, "chunks"), 2, "chunks") == items
    assert collection_bytes([], 1, "chunks") == b"[]"
    with pytest.raises(ValueError, match="max_items"):
        validate_collection(items, 1, "chunks")


def test_collection_byte_limit() -> None:
    items = [{"x": "x" * (COLLECTION_BYTE_LIMIT - len(b'[{"x":""}]'))}]
    assert len(collection_bytes(items, 1, "chunks")) == COLLECTION_BYTE_LIMIT
    items[0]["x"] += "x"
    with pytest.raises(ValueError, match="byte limit"):
        validate_collection(items, 1, "chunks")
    with pytest.raises(ValueError, match="byte limit"):
        decode_collection(b" " * (COLLECTION_BYTE_LIMIT + 1), 1, "chunks")


def test_depth_and_cycles() -> None:
    item: dict[str, object] = {}
    items = [item]
    for depth in range(62):
        nested: dict[str, object] = {}
        item[str(depth)] = nested
        item = nested
    validate_collection(items, 1, "chunks")
    item["x"] = {}
    with pytest.raises(ValueError, match="depth"):
        validate_collection(items, 1, "chunks")
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cycle"):
        validate_collection([{"x": cycle}], 1, "chunks")


def test_validation_preserves_identity_and_shared_containers() -> None:
    shared = {"values": [1, True, None, "text"]}
    item = {"second": shared, "first": shared}
    items = [item, item]

    assert validate_collection(items, 2, "chunks") is items
    assert items[0] is items[1] is item
    assert item["second"] is item["first"] is shared
    assert list(item) == ["second", "first"]
    assert shared == {"values": [1, True, None, "text"]}


@pytest.mark.parametrize(
    "value,message",
    [
        (
            [{"second": [{"bad": math.inf}], "first": b"bad"}],
            "Foreach 'chunks' planner.findings[0].second[0].bad: numbers must be finite",
        ),
        (
            [{"nested": {1: b"bad"}}],
            "Foreach 'chunks' planner.findings[0].nested: object keys must be strings",
        ),
        (
            [{"nested": {"\ud800": b"bad"}}],
            "Foreach 'chunks' planner.findings[0].nested: invalid UTF-8 string",
        ),
        (
            [{"nested": [{"key": b"bad"}, math.nan]}],
            "Foreach 'chunks' planner.findings[0].nested[0].key: unsupported JSON value bytes",
        ),
        (
            [{"bad": math.inf}, 0],
            "Foreach 'chunks' planner.findings[1]: expected an object",
        ),
    ],
)
def test_validation_preserves_first_error_and_label(
    value: object, message: str
) -> None:
    with pytest.raises(ValueError) as error:
        validate_collection(value, 2, "chunks", "planner.findings")

    assert str(error.value) == message


@pytest.mark.parametrize("container_kind", ["dict", "list"])
def test_nested_cycles_report_the_repeated_container(container_kind: str) -> None:
    container: dict[str, object] | list[object]
    if container_kind == "dict":
        container = {}
        container["self"] = container
        path = ".self"
    else:
        container = []
        container.append(container)
        path = "[0]"

    with pytest.raises(ValueError) as error:
        validate_collection([{"nested": container}], 1, "chunks")

    assert str(error.value) == (
        f"Foreach 'chunks' items[0].nested{path}: collection contains a cycle"
    )


@pytest.mark.parametrize("value", [[math.nan], {1: 0}])
def test_failed_validation_leaves_shared_collection_reusable(value: object) -> None:
    item = {"nested": value}
    items = [item, item]

    with pytest.raises(ValueError):
        validate_collection(items, 2, "chunks")

    assert items[0] is items[1] is item
    assert item["nested"] is value
    item["nested"] = {"valid": [1]}
    assert validate_collection(items, 2, "chunks") is items
