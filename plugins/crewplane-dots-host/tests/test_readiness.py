import os
import subprocess
import sys
from time import monotonic

import pytest

from .readiness import read_job_receipt


@pytest.mark.parametrize(
    ("payload", "hold", "category"),
    [
        (b"", True, "timeout"),
        (b"dots-host job partial", True, "incomplete"),
        (b"", False, "EOF"),
        (b"dots-host job partial", False, "incomplete"),
        (b"x" * 129, True, "overlong"),
        (b"unrelated output\n", True, "malformed"),
        (b"dots-host job id-123\ndiagnostic\n", True, None),
    ],
)
def test_readiness_receipt_is_bounded_and_preserves_diagnostics(
    payload, hold, category
):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys; os.write(2, bytes.fromhex(sys.argv[1])); "
            "sys.stdin.read() if sys.argv[2] == 'hold' else None",
            payload.hex(),
            "hold" if hold else "exit",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    start = monotonic()
    try:
        if category:
            with pytest.raises(AssertionError, match=category):
                read_job_receipt(process.stderr, 0.5)
        else:
            assert read_job_receipt(process.stderr, 5) == "id-123"
            assert (
                os.read(process.stderr.fileno(), len(b"diagnostic\n"))
                == b"diagnostic\n"
            )
        assert monotonic() - start < 5
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    assert process.returncode is not None
    assert all(
        stream.closed for stream in (process.stdin, process.stdout, process.stderr)
    )


def test_readiness_child_is_reaped_and_pipes_close_after_assertion_failure():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys; os.write(2,b'dots-host job id-123\\n'); sys.stdin.read()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    with pytest.raises(AssertionError, match="injected assertion"):
        try:
            assert read_job_receipt(process.stderr, 5) == "id-123"
            raise AssertionError("injected assertion")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
    assert process.returncode is not None
    assert all(
        stream.closed for stream in (process.stdin, process.stdout, process.stderr)
    )
