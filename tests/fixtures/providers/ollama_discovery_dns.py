"""Enter a stalled OS resolver in the real discovery worker."""

import os
import socket
import sys
import threading
from pathlib import Path

from crewplane_ollama.discovery_worker import main

receipts = Path(sys.argv[1])
sys.argv[1:] = [sys.argv[2]]


def stalled_resolver(*args, **kwargs):
    del args, kwargs
    (receipts / f"ready-{os.getpid()}").touch()
    threading.Event().wait(60)
    raise OSError("fixture DNS stall expired")


socket.getaddrinfo = stalled_resolver
main()
