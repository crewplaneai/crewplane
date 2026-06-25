PYTHON ?= python
PYPI_REPOSITORY ?= pypi
TWINE_UPLOAD_ARGS ?=
NPM_OTP ?=
NPM_PUBLISH_OTP ?=
NPM_DIST_TAG_OTP ?=
NPM_PUBLISH_ARGS ?=
HAVE_UV := $(shell if command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1; then echo 1; else echo 0; fi)
PROJECT_NAME_CMD = $(PYTHON) -c 'import sys, tomllib; project = tomllib.load(open("pyproject.toml", "rb"))["project"]; name = project["name"]; scripts = list(project.get("scripts", {})); sys.exit(f"expected one [project.scripts] key matching project.name {name!r}, got {scripts!r}") if scripts != [name] else print(name)'
PACKAGE_NAME := $(shell $(PROJECT_NAME_CMD))
ifneq ($(.SHELLSTATUS),0)
$(error failed to derive package name from pyproject.toml)
endif

ifeq ($(HAVE_UV),1)
INSTALL_CMD = uv sync --extra dev
UNINSTALL_CMD = uv pip uninstall $(PACKAGE_NAME)
RUN_PYTHON = uv run --extra dev python
RUN_PYTEST = uv run --extra dev python -m pytest -q
RUN_RUFF = uv run --extra dev python -m ruff
else
INSTALL_CMD = $(PYTHON) -m pip install -e '.[dev]'
UNINSTALL_CMD = $(PYTHON) -m pip uninstall $(PACKAGE_NAME)
RUN_PYTHON = $(PYTHON)
RUN_PYTEST = $(PYTHON) -m pytest -q
RUN_RUFF = $(PYTHON) -m ruff
endif

RUN_RELEASE = $(RUN_PYTHON) scripts/release.py

.PHONY: help setup uninstall test lint format format-check check clean \
	package-build package-check package-wheelhouse changelog-check \
	install-smoke-pip install-smoke-uv install-smoke-pipx install-smoke \
	install-script-smoke npm-pack npm-smoke brew-smoke install-check \
	release-prepare release-check release-confirm release-pypi release-npm release

.NOTPARALLEL: release-prepare release-check release

help:
	@printf '%s\n' \
		'Usage: make <target>' \
		'' \
		'Development:' \
		'  setup              Install editable dev environment' \
		'  test               Run pytest' \
		'  lint               Run ruff checks' \
		'  format             Run ruff import fixes and formatter' \
		'  format-check       Check formatting' \
		'  check              Run lint, format-check, and tests' \
		'  clean              Remove caches, build output, and release scratch files' \
		'  uninstall          Uninstall the package from the active environment' \
		'' \
		'Package validation:' \
		'  package-build      Build wheel and sdist into dist/' \
		'  package-check      Build artifacts, run twine check, verify Homebrew sdist SHA' \
		'  install-smoke      Exercise pip, uv tool, and pipx installs where available' \
		'  install-check      Run package checks plus installer, npm, and Homebrew smokes' \
		'' \
		'Normal release flow:' \
		'  release-prepare    Sync generated metadata and prepare local release artifacts' \
		'  release-check      Run pre-publish checks, or verify a completed release no-op' \
		'  release            Confirm, check, publish PyPI, publish npm latest, and tag' \
		'' \
		'Partial completion:' \
		'  release-pypi       Publish or verify prepared PyPI artifacts only' \
		'  release-npm        Publish or verify the npm wrapper and reconcile npm latest' \
		'' \
		'Release variables:' \
		'  Release version is read from pyproject.toml' \
		'  PYPI_REPOSITORY    Twine repository name (default: pypi)' \
		'  TWINE_UPLOAD_ARGS  Extra arguments passed to twine upload' \
		'  NPM_OTP            npm one-time password for one npm operation' \
		'  NPM_PUBLISH_OTP    npm one-time password for npm publish in non-TTY mode' \
		'  NPM_DIST_TAG_OTP   npm one-time password for npm dist-tag add in non-TTY mode' \
		'  NPM_PUBLISH_ARGS   Extra arguments passed to npm publish and dist-tag' \
		'' \
		'Homebrew tap publishing is separate: copy the prepared formula into the tap, audit/test there, and push the tap update.'

setup:
	$(INSTALL_CMD)

uninstall:
	$(UNINSTALL_CMD)

test:
	$(RUN_PYTEST)

lint:
	$(RUN_RUFF) check src tests scripts

format:
	$(RUN_RUFF) check --fix --select I src tests scripts
	$(RUN_RUFF) format src tests scripts

format-check:
	$(RUN_RUFF) format --check src tests scripts

check: lint format-check test

package-build:
	$(RUN_RELEASE) package-build

package-check:
	$(RUN_RELEASE) package-check

package-wheelhouse:
	$(RUN_RELEASE) package-wheelhouse

changelog-check:
	$(RUN_RELEASE) changelog-check

install-smoke-pip:
	$(RUN_RELEASE) install-smoke-pip

install-smoke-uv:
	$(RUN_RELEASE) install-smoke-uv

install-smoke-pipx:
	$(RUN_RELEASE) install-smoke-pipx

install-smoke:
	$(RUN_RELEASE) install-smoke

install-script-smoke:
	$(RUN_RELEASE) install-script-smoke

npm-pack:
	$(RUN_RELEASE) npm-pack

npm-smoke:
	$(RUN_RELEASE) npm-smoke

brew-smoke:
	$(RUN_RELEASE) brew-smoke

install-check:
	$(RUN_RELEASE) install-check

release-prepare:
	$(RUN_RELEASE) prepare

release-check:
	$(RUN_RELEASE) check

release-confirm:
	$(RUN_RELEASE) confirm

release-pypi:
	$(RUN_RELEASE) publish-pypi --execute

release-npm:
	$(RUN_RELEASE) publish-npm --execute

release: release-confirm release-check
	$(MAKE) release-pypi
	$(MAKE) release-npm
	$(RUN_RELEASE) finalize --execute

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info .release .release-manifests
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
