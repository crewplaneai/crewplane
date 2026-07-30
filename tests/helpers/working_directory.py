import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def temporary_project_cwd() -> Iterator[Path]:
    original_cwd = Path.cwd()

    with tempfile.TemporaryDirectory() as tmp_dir:
        project_root = Path(tmp_dir).resolve()
        os.chdir(project_root)
        try:
            yield project_root
        finally:
            os.chdir(original_cwd)
