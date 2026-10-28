PYTHON ?= python
PYPI_REPOSITORY ?= pypi
TWINE_UPLOAD_ARGS ?=
NPM_OTP ?=
NPM_PUBLISH_OTP ?=
NPM_DIST_TAG_OTP ?=
NPM_PUBLISH_ARGS ?=
UV_BOOTSTRAP_VERSION ?= latest
PROJECT_NAME_CMD = $(PYTHON) -c 'import sys, tomllib; project = tomllib.load(open("pyproject.toml", "rb"))["project"]; name = project["name"]; scripts = list(project.get("scripts", {})); sys.exit(f"expected one [project.scripts] key matching project.name {name!r}, got {scripts!r}") if scripts != [name] else print(name)'
PACKAGE_NAME := $(shell $(PROJECT_NAME_CMD) || printf '%s\n' __PACKAGE_NAME_LOOKUP_FAILED__)
ifeq ($(PACKAGE_NAME),__PACKAGE_NAME_LOOKUP_FAILED__)
$(error failed to derive package name from pyproject.toml)
endif

TEST_PROFILE ?= $(shell $(PYTHON) scripts/validation_policy.py --get runner.default_profile)
TEST_ARGS ?=
COVERAGE_REPORT ?=
TEST_EXTRAS = --extra dev $(if $(filter pr-313 nightly-shuffle,$(TEST_PROFILE)),--extra stress,)
INSTALL_CMD = uv sync --locked $(TEST_EXTRAS)
UNINSTALL_CMD = uv pip uninstall $(PACKAGE_NAME)
RUN_PYTHON = uv run --locked $(TEST_EXTRAS) python
RUN_RUFF = $(RUN_PYTHON) -m ruff
RUN_MYPY = $(RUN_PYTHON) -m mypy
PLUGIN_SOURCES = $(wildcard plugins/*/src)
OLLAMA_PACKAGE = plugins/crewplane-ollama

RUN_RELEASE = $(RUN_PYTHON) scripts/release.py
RUN_PACKAGE = $(RUN_PYTHON) scripts/run_package_checks.py -- $(RUN_RELEASE)
RUN_TESTS = $(RUN_PYTHON) scripts/run_tests.py --profile $(TEST_PROFILE) $(if $(STATEMENT_COVERAGE_FLOOR),--statement-floor $(STATEMENT_COVERAGE_FLOOR)) $(if $(BRANCH_COVERAGE_FLOOR),--branch-floor $(BRANCH_COVERAGE_FLOOR))

.PHONY: help setup uninstall test coverage-check typecheck lint format format-check check actionlint \
	provider-defaults-check dependency-audit uv-bootstrap-check uv-bootstrap-update clean \
	package-build package-check package-wheelhouse wheel-typecheck changelog-check \
	ollama-package-build ollama-package-check \
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
		'  test               Run pytest with statement and branch coverage gates' \
		'  provider-defaults-check Check template model defaults and propagation' \
		'  coverage-check    Check floors in an explicit COVERAGE_REPORT' \
		'  typecheck          Check strict typing for core, plugins, and fixtures' \
		'  lint               Run ruff checks' \
		'  format             Run ruff import fixes and formatter' \
		'  format-check       Check formatting' \
		'  check              Run all development checks' \
		'  actionlint         Validate GitHub Actions workflows' \
		'  dependency-audit   Audit locked and build-system Python dependencies' \
		'  uv-bootstrap-check Verify pinned uv metadata is synchronized' \
		'  uv-bootstrap-update Update pinned uv metadata (default: latest)' \
		'  clean              Remove caches, build output, and release scratch files' \
		'  uninstall          Uninstall the package from the active environment' \
		'' \
		'Package validation:' \
		'  package-build      Build core and default plugin distributions into dist/' \
		'  package-check      Build artifacts, run twine check, verify Homebrew sdist SHA' \
		'  ollama-package-build Rebuild the Ollama wheel and sdist in its dist/' \
		'  ollama-package-check Build and validate core and Ollama distributions' \
		'  wheel-typecheck    Type-check an external consumer against the built wheel' \
		'  install-smoke      Exercise pip, uv tool, and pipx installs where available' \
		'  install-check      Build once, then run package, installer, npm, and Homebrew smokes' \
		'' \
		'Normal release flow:' \
		'  release-prepare    Sync generated metadata and prepare local release artifacts' \
		'  release-check      Verify changelog and pre-publish checks, or a completed release' \
		'  release            Confirm, publish PyPI, publish npm latest, and tag' \
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
		'Eligible GitHub releases open a Homebrew tap PR; publish it with the tap brew pr-pull workflow after checks pass.'

setup:
	$(INSTALL_CMD)

uninstall:
	$(UNINSTALL_CMD)

test:
	$(RUN_TESTS) $(TEST_ARGS)

provider-defaults-check:
	$(RUN_TESTS) --coverage off \
		tests/unit/cli/test_onboarding_rendering.py \
		tests/integration/cli/test_template_provider_defaults.py $(TEST_ARGS)

coverage-check:
	@test -n "$(COVERAGE_REPORT)" || (printf '%s\n' 'Set COVERAGE_REPORT to a test-results/<invocation>/coverage.json file.'; exit 1)
	$(RUN_PYTHON) scripts/check_coverage.py "$(COVERAGE_REPORT)" $(if $(STATEMENT_COVERAGE_FLOOR),--statements $(STATEMENT_COVERAGE_FLOOR)) $(if $(BRANCH_COVERAGE_FLOOR),--branches $(BRANCH_COVERAGE_FLOOR))

# Keep the full production package and repository typecheck fixtures under
# strict mypy coverage.
typecheck:
	$(RUN_MYPY) src/crewplane tests/typecheck $(PLUGIN_SOURCES)

lint:
	$(RUN_RUFF) check src tests scripts $(PLUGIN_SOURCES)

format:
	$(RUN_RUFF) check --fix --select I src tests scripts $(PLUGIN_SOURCES)
	$(RUN_RUFF) format src tests scripts $(PLUGIN_SOURCES)

format-check:
	$(RUN_RUFF) format --check src tests scripts $(PLUGIN_SOURCES)

check: lint format-check typecheck uv-bootstrap-check
	$(RUN_TESTS) --suite all $(TEST_ARGS)

.PHONY: dots-host-check
dots-host-check:
	$(RUN_TESTS) --suite dots $(TEST_ARGS)

actionlint:
	scripts/actionlint.sh

dependency-audit:
	$(PYTHON) scripts/dependency_audit.py

uv-bootstrap-check:
	$(RUN_PYTHON) scripts/update_uv_bootstrap.py check

uv-bootstrap-update:
	$(RUN_PYTHON) scripts/update_uv_bootstrap.py update $(UV_BOOTSTRAP_VERSION)

package-build:
	$(RUN_PACKAGE) package-build

ollama-package-build:
	rm -rf $(OLLAMA_PACKAGE)/dist
	$(RUN_PYTHON) scripts/run_package_checks.py -- $(RUN_PYTHON) -m build --outdir $(OLLAMA_PACKAGE)/dist $(OLLAMA_PACKAGE)

ollama-package-check:
	$(RUN_PYTHON) scripts/run_package_checks.py --profile plugins

package-check:
	$(RUN_PACKAGE) package-check

package-wheelhouse:
	$(RUN_PACKAGE) package-wheelhouse

wheel-typecheck:
	$(RUN_PYTHON) scripts/run_package_checks.py -- $(RUN_PYTHON) scripts/check_wheel_consumer_types.py $$(find dist -maxdepth 1 -type f -name 'crewplane-*.whl' -print -quit)

changelog-check:
	$(RUN_RELEASE) changelog-check

install-smoke-pip:
	$(RUN_PACKAGE) install-smoke-pip

install-smoke-uv:
	$(RUN_PACKAGE) install-smoke-uv

install-smoke-pipx:
	$(RUN_PACKAGE) install-smoke-pipx

install-smoke:
	$(RUN_PACKAGE) install-smoke

install-script-smoke:
	$(RUN_PACKAGE) install-script-smoke

npm-pack:
	$(RUN_PACKAGE) npm-pack

npm-smoke:
	$(RUN_PACKAGE) npm-smoke

brew-smoke:
	$(RUN_PACKAGE) brew-smoke

install-check:
	$(RUN_PACKAGE) install-check

release-prepare:
	uv lock
	$(RUN_RELEASE) prepare

release-check:
	$(RUN_RELEASE) check

release-confirm:
	$(RUN_RELEASE) confirm

release-pypi:
	$(RUN_RELEASE) publish-pypi --execute

release-npm:
	$(RUN_RELEASE) publish-npm --execute

release: release-confirm
	$(MAKE) release-pypi
	$(MAKE) release-npm
	$(RUN_RELEASE) finalize --execute

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info .release .release-manifests
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
