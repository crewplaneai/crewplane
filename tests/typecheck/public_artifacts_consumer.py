from __future__ import annotations

from typing import assert_type

from crewplane.artifacts import FindingsExtractionError, OutputManager

assert_type(OutputManager("workflow"), OutputManager)

try:
    raise FindingsExtractionError("invalid findings")
except FindingsExtractionError as error:
    assert_type(error, FindingsExtractionError)
