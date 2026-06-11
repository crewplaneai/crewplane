PYTHON ?= python
HAVE_UV := $(shell if command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1; then echo 1; else echo 0; fi)

ifeq ($(HAVE_UV),1)
INSTALL_CMD = uv sync --extra dev
UNINSTALL_CMD = uv pip uninstall orchestrator-cli
RUN_PYTEST = uv run --extra dev python -m pytest -q
RUN_RUFF = uv run --extra dev python -m ruff
else
INSTALL_CMD = $(PYTHON) -m pip install -e '.[dev]'
UNINSTALL_CMD = $(PYTHON) -m pip uninstall orchestrator-cli
RUN_PYTEST = $(PYTHON) -m pytest -q
RUN_RUFF = $(PYTHON) -m ruff
endif

.PHONY: setup uninstall test lint format format-check check clean

setup:
	$(INSTALL_CMD)

uninstall:
	$(UNINSTALL_CMD)

test:
	$(RUN_PYTEST)

lint:
	$(RUN_RUFF) check src tests

format:
	$(RUN_RUFF) check --fix --select I src tests
	$(RUN_RUFF) format src tests

format-check:
	$(RUN_RUFF) format --check src tests

check: lint format-check test

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
