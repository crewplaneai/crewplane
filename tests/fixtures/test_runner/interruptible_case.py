import time
from pathlib import Path


def test_block() -> None:
    Path("ready").touch()
    time.sleep(60)
