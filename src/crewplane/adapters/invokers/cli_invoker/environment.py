import os
import shutil
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path


def environment_executable_lookup(
    environment: Mapping[str, str], root: Path, fallback: Callable[[str], str | None]
) -> Callable[[str], str | None]:
    if "PATH" not in environment:
        return fallback
    search_path = os.pathsep.join(
        str(root / entry) for entry in environment["PATH"].split(os.pathsep)
    )
    return partial(shutil.which, path=search_path)
