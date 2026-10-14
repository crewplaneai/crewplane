import os
from pathlib import Path


def pytest_sessionstart() -> None:
    Path(f"session-{os.getpid()}.pid").write_text(str(os.getpid()), encoding="utf-8")
