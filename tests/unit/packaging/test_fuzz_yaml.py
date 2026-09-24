import runpy
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_yaml_fuzzer_rejects_invalid_scalars_and_propagates_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "atheris",
        SimpleNamespace(
            instrument_imports=nullcontext,
            instrument_func=lambda function: function,
        ),
    )
    fuzz_one_input = runpy.run_path(str(ROOT / "fuzz" / "fuzz_yaml.py"))[
        "fuzz_one_input"
    ]

    fuzz_one_input(b"2001-13-01")

    monkeypatch.setitem(
        fuzz_one_input.__globals__,
        "load_yaml_unique",
        Mock(side_effect=RuntimeError("unexpected loader failure")),
    )
    with pytest.raises(RuntimeError, match="unexpected loader failure"):
        fuzz_one_input(b"key: value")
