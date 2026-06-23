from __future__ import annotations

from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate mapping keys at any nesting level."""


def _construct_mapping_with_unique_keys(
    loader: UniqueKeyLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            f"expected a mapping node, but found {node.id}",
            node.start_mark,
        )

    keys: set[object] = set()
    for mapping_entry in node.value:
        key_node = mapping_entry[0]
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in keys:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate YAML key {key!r}",
                    key_node.start_mark,
                )
            keys.add(key)
        except TypeError as error:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable YAML key",
                key_node.start_mark,
            ) from error

    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_with_unique_keys,
)


def load_yaml_unique(text: str) -> Any:
    """Load YAML text while rejecting duplicate mapping keys."""

    return yaml.load(text, Loader=UniqueKeyLoader)
