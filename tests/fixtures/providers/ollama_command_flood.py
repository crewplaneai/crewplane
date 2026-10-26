"""Exercise simultaneous command stdin, stdout, and stderr pressure."""

import os
import sys
import threading


def emit(descriptor: int, count: int, prefix: bytes) -> None:
    os.write(descriptor, prefix)
    chunk = b"x" * 16384
    while count:
        size = min(count, len(chunk))
        os.write(descriptor, chunk[:size])
        count -= size


def main() -> None:
    count = int(sys.argv[1])
    streams = [
        threading.Thread(target=emit, args=(1, count, b"head-out:")),
        threading.Thread(target=emit, args=(2, count, b"head-err:")),
    ]
    for stream in streams:
        stream.start()
    received = len(sys.stdin.buffer.read())
    for stream in streams:
        stream.join()
    os.write(1, f":tail-out:{received}".encode())
    os.write(2, b":tail-err")


if __name__ == "__main__":
    main()
