from __future__ import annotations

from pathlib import Path

from ..naming import build_generated_file_result_dir_name


def generated_file_node_prefix(node_id: str) -> Path:
    return Path("generated-files") / build_generated_file_result_dir_name(node_id)


def generated_file_path_belongs_to_node(relative_path: str, node_id: str) -> bool:
    parts = relative_path.split("/")
    return (
        len(parts) >= 4
        and tuple(parts[:2]) == generated_file_node_prefix(node_id).parts
        and all(part not in {"", ".", ".."} for part in parts)
    )
