import fcntl
import sys
import time
from pathlib import Path

with Path(sys.argv[1]).open("a+b") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    print("locked", flush=True)
    time.sleep(float(sys.argv[2]))
