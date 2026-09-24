"""Fuzz Crewplane's YAML loader without executing workflows."""

import sys

import atheris

with atheris.instrument_imports():
    import yaml

    from crewplane.core.yaml_loader import load_yaml_unique


@atheris.instrument_func
def fuzz_one_input(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return

    try:
        load_yaml_unique(text)
    except (yaml.YAMLError, ValueError):
        # Rejecting malformed YAML is expected.
        return

    # Unexpected exceptions intentionally fail the fuzz run.


if __name__ == "__main__":
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()
