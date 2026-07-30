from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from crewplane.artifacts.atomic import atomic_write_text


def _crash_atomic_writer(target_text: str, phase: str) -> None:
    target = Path(target_text)
    original_replace = Path.replace

    def crashing_replace(source: Path, destination: Path) -> Path:
        if phase == "before_replace":
            os._exit(86)
        original_replace(source, destination)
        os._exit(87)

    Path.replace = crashing_replace
    atomic_write_text(target, "new publication\n")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process crash semantics required")
@pytest.mark.parametrize(
    ("phase", "expected_exit", "expected_content"),
    [
        ("before_replace", 86, "old publication\n"),
        ("after_replace", 87, "new publication\n"),
    ],
)
def test_atomic_publication_is_never_partial_across_process_crash_points(
    tmp_path: Path,
    phase: str,
    expected_exit: int,
    expected_content: str,
) -> None:
    target = tmp_path / "result.json"
    target.write_text("old publication\n", encoding="utf-8")
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_atomic_writer,
        args=(target.as_posix(), phase),
    )

    process.start()
    process.join(timeout=10)

    assert not process.is_alive()
    assert process.exitcode == expected_exit
    assert target.read_text(encoding="utf-8") == expected_content
