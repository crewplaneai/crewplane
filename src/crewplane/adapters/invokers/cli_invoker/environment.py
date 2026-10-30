import os
import shutil
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path


def rebase_search_path(search_path: str, root: Path) -> str:
    return os.pathsep.join(str(root / entry) for entry in search_path.split(os.pathsep))


def environment_executable_lookup(
    environment: Mapping[str, str], root: Path, fallback: Callable[[str], str | None]
) -> Callable[[str], str | None]:
    if "PATH" not in environment:
        return fallback
    search_path = rebase_search_path(environment["PATH"], root)
    return partial(shutil.which, path=search_path)
