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
