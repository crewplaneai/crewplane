from collections.abc import Iterator
from contextlib import chdir, contextmanager
from pathlib import Path
from tempfile import mkdtemp


@contextmanager
def temporary_project_cwd(tmp_path: Path) -> Iterator[Path]:
    project_root = Path(mkdtemp(dir=tmp_path)).resolve()
    with chdir(project_root):
        yield project_root
