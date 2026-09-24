import hashlib
import resource
import tempfile
from pathlib import Path

from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)


def main() -> None:
    _, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    soft_limit = 64 if hard_limit == resource.RLIM_INFINITY else min(64, hard_limit)
    if soft_limit < 32:
        raise RuntimeError("Descriptor limit is too low for this regression test.")
    resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, hard_limit))
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        source = root_path / "source.bin"
        source.write_bytes(b"x")
        signature = (1, hashlib.sha256(b"x").hexdigest())
        registry = RuntimePublicationRegistry()
        for index in range(128):
            registry.publish(
                root_path / f"publication-{index}.bin",
                signature,
                recovery_source=source,
            )
        registry.close()
    print(index + 1)


if __name__ == "__main__":
    main()
