PYTHON ?= python
PACKAGE_NAME := crewplane
CLI_NAME := orchestrator
WHEELHOUSE := $(CURDIR)/.release/wheelhouse
NPM_PACK_DIR := $(CURDIR)/.release/npm
PYPI_REPOSITORY ?= pypi
TWINE_UPLOAD_ARGS ?=
NPM_TAG ?= alpha
NPM_PUBLISH_ARGS ?=
HAVE_UV := $(shell if command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1; then echo 1; else echo 0; fi)

ifeq ($(HAVE_UV),1)
INSTALL_CMD = uv sync --extra dev
UNINSTALL_CMD = uv pip uninstall $(PACKAGE_NAME)
RUN_PYTHON = uv run --extra dev python
RUN_PIP = uv run --extra dev --with pip python -m pip
RUN_PYTEST = uv run --extra dev python -m pytest -q
RUN_RUFF = uv run --extra dev python -m ruff
else
INSTALL_CMD = $(PYTHON) -m pip install -e '.[dev]'
UNINSTALL_CMD = $(PYTHON) -m pip uninstall $(PACKAGE_NAME)
RUN_PYTHON = $(PYTHON)
RUN_PIP = $(PYTHON) -m pip
RUN_PYTEST = $(PYTHON) -m pytest -q
RUN_RUFF = $(PYTHON) -m ruff
endif

PROJECT_VERSION_CMD = $(RUN_PYTHON) -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])'
PROJECT_VERSION = $(shell $(PROJECT_VERSION_CMD))
NORMALIZE_VERSION = $(RUN_PYTHON) -c 'from packaging.version import Version; print(Version("$(PROJECT_VERSION)"))'
RELEASE_CHECKS = $(RUN_PYTHON) packaging/release_checks.py

.PHONY: help setup uninstall test lint format format-check check clean \
	package-build package-check package-wheelhouse changelog-check \
	release-version-check release-remote-version-check release-confirm \
	install-smoke-pip install-smoke-uv install-smoke-pipx install-smoke \
	install-script-smoke npm-pack npm-smoke brew-smoke install-check \
	release-check release-prereqs release-pypi release-npm release

.NOTPARALLEL: release-check release

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
		'  release-check      Check local metadata, remote availability, tests, and packages' \
		'' \
		'Publishing:' \
		'  release            Confirm version, run release checks, publish PyPI, then npm' \
		'  release-pypi       Upload dist artifacts with twine' \
		'  release-npm        Pack and publish the npm wrapper' \
		'  release-prereqs    Require npm and npm authentication before release' \
		'' \
		'Release variables:' \
		'  Release version is read from pyproject.toml' \
		'  PYPI_REPOSITORY    Twine repository name (default: pypi)' \
		'  TWINE_UPLOAD_ARGS  Extra arguments passed to twine upload' \
		'  NPM_TAG            npm publish dist-tag (default: alpha)' \
		'  NPM_PUBLISH_ARGS   Extra arguments passed to npm publish' \
		'' \
		'Homebrew tap publishing is separate: regenerate pins from the canonical PyPI artifact, copy the formula into the tap, and validate it there.'

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

package-build:
	@set -eu; \
	version="$(PROJECT_VERSION)"; \
	echo "Building $(PACKAGE_NAME) $$version from pyproject.toml"; \
	rm -rf dist build *.egg-info; \
	$(RUN_PYTHON) -m build --sdist --wheel --outdir dist

package-check: package-build
	@set -eu; \
	normalized="$$( $(NORMALIZE_VERSION) )"; \
	sdist="$(CURDIR)/dist/$(PACKAGE_NAME)-$${normalized}.tar.gz"; \
	wheel="$(CURDIR)/dist/$(PACKAGE_NAME)-$${normalized}-py3-none-any.whl"; \
	test -f "$$sdist"; \
	test -f "$$wheel"; \
	$(RUN_PYTHON) -m twine check "$$sdist" "$$wheel"; \
	formula_sha="$$(awk '/sha256 "/ { gsub(/"/, "", $$2); print $$2; exit }' packaging/homebrew/Formula/crewplane.rb)"; \
	sdist_sha="$$(shasum -a 256 "$$sdist" | awk '{print $$1}')"; \
	if [ "$$formula_sha" != "$$sdist_sha" ]; then \
		echo "Homebrew formula source SHA $$formula_sha does not match $$sdist_sha"; \
		exit 1; \
	fi

package-wheelhouse: package-build
	@set -eu; \
	normalized="$$( $(NORMALIZE_VERSION) )"; \
	wheel="$(CURDIR)/dist/$(PACKAGE_NAME)-$${normalized}-py3-none-any.whl"; \
	test -f "$$wheel"; \
	rm -rf "$(WHEELHOUSE)"; \
	mkdir -p "$(WHEELHOUSE)"; \
	if command -v uv >/dev/null 2>&1; then \
		mkdir -p "$(CURDIR)/.release"; \
		uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements.txt --output-file "$(CURDIR)/.release/runtime-requirements.txt" >/dev/null; \
		$(RUN_PIP) download --dest "$(WHEELHOUSE)" -r "$(CURDIR)/.release/runtime-requirements.txt" "$$wheel"; \
	else \
		$(RUN_PIP) download --dest "$(WHEELHOUSE)" "$$wheel"; \
	fi

changelog-check:
	@set -eu; \
	version="$(PROJECT_VERSION)"; \
	grep -Eq "^## \\[$$version\\]|^## $$version" CHANGELOG.md

release-version-check:
	$(RELEASE_CHECKS) local --version "$(PROJECT_VERSION)"

release-remote-version-check:
	$(RELEASE_CHECKS) remote --package-name "$(PACKAGE_NAME)" --version "$(PROJECT_VERSION)"

release-confirm:
	@set -eu; \
	printf '%s' "Release $(PACKAGE_NAME) $(PROJECT_VERSION) to PyPI repository '$(PYPI_REPOSITORY)' and npm tag '$(NPM_TAG)'? [y/N] "; \
	if ! read answer; then \
		echo "No confirmation received; aborting."; \
		exit 1; \
	fi; \
	case "$$answer" in \
		[Yy]) ;; \
		*) echo "Aborting release."; exit 1 ;; \
	esac

install-smoke-pip: package-wheelhouse
	@set -eu; \
	smoke_python="$$( $(RUN_PYTHON) -c 'import sys; print(sys.executable)' )"; \
	tmp="$$(mktemp -d)"; \
	trap 'rm -rf "$$tmp"' EXIT; \
	if command -v uv >/dev/null 2>&1; then \
		uv venv --seed --python "$$smoke_python" "$$tmp/venv" >/dev/null; \
	else \
		$(PYTHON) -m venv "$$tmp/venv"; \
	fi; \
	"$$tmp/venv/bin/python" -m pip install --no-index --find-links "$(WHEELHOUSE)" "$(PACKAGE_NAME)==$(PROJECT_VERSION)" >/dev/null; \
	exe="$$tmp/venv/bin/$(CLI_NAME)"; \
	"$$exe" --help >/dev/null; \
	project="$$tmp/project"; \
	mkdir -p "$$project"; \
	( cd "$$project" && "$$exe" init >/dev/null ); \
	printf '%s\n' \
		'version: "1.0"' \
		'agents:' \
		'  claude:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'  codex:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'  gemini:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'settings:' \
		'  integrations:' \
		'    invoker:' \
		'      implementation: "mock"' \
		'      options:' \
		'        delay_seconds: 0' \
		'        observation_delay_seconds: 0' \
		'        output_mode: "lorem"' \
		'    ui:' \
		'      implementation: "none"' \
		'      options: {}' \
		'    artifacts:' \
		'      implementation: "filesystem"' \
		'      options:' \
		'        log_cli_output: true' \
		'        allowed_template_paths: []' \
		> "$$project/.orchestrator/config.yml"; \
	( cd "$$project" && "$$exe" validate >/dev/null )

install-smoke-uv: package-wheelhouse
	@set -eu; \
	if ! command -v uv >/dev/null 2>&1; then \
		echo "Skipping install-smoke-uv: uv not found."; \
		exit 0; \
	fi; \
	smoke_python="$$( $(RUN_PYTHON) -c 'import sys; print(sys.executable)' )"; \
	tmp="$$(mktemp -d)"; \
	trap 'rm -rf "$$tmp"' EXIT; \
	HOME="$$tmp/home" uv tool install --force --python "$$smoke_python" --find-links "$(WHEELHOUSE)" --no-index "$(PACKAGE_NAME)==$(PROJECT_VERSION)" >/dev/null; \
	tool_bin="$$(HOME="$$tmp/home" uv tool dir --bin)"; \
	exe="$$tool_bin/$(CLI_NAME)"; \
	"$$exe" --help >/dev/null; \
	project="$$tmp/project"; \
	mkdir -p "$$project"; \
	( cd "$$project" && "$$exe" init >/dev/null ); \
	printf '%s\n' \
		'version: "1.0"' \
		'agents:' \
		'  claude:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'  codex:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'  gemini:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'settings:' \
		'  integrations:' \
		'    invoker:' \
		'      implementation: "mock"' \
		'      options:' \
		'        delay_seconds: 0' \
		'        observation_delay_seconds: 0' \
		'        output_mode: "lorem"' \
		'    ui:' \
		'      implementation: "none"' \
		'      options: {}' \
		'    artifacts:' \
		'      implementation: "filesystem"' \
		'      options:' \
		'        log_cli_output: true' \
		'        allowed_template_paths: []' \
		> "$$project/.orchestrator/config.yml"; \
	( cd "$$project" && "$$exe" validate >/dev/null )

install-smoke-pipx: package-wheelhouse
	@set -eu; \
	if ! command -v pipx >/dev/null 2>&1; then \
		echo "Skipping install-smoke-pipx: pipx not found."; \
		exit 0; \
	fi; \
	smoke_python="$$( $(RUN_PYTHON) -c 'import sys; print(sys.executable)' )"; \
	tmp="$$(mktemp -d)"; \
	trap 'rm -rf "$$tmp"' EXIT; \
	PIPX_HOME="$$tmp/pipx-home" PIPX_BIN_DIR="$$tmp/bin" \
		pipx install --force --python "$$smoke_python" --pip-args="--no-index --find-links $(WHEELHOUSE)" "$(PACKAGE_NAME)==$(PROJECT_VERSION)" >/dev/null; \
	exe="$$tmp/bin/$(CLI_NAME)"; \
	"$$exe" --help >/dev/null; \
	project="$$tmp/project"; \
	mkdir -p "$$project"; \
	( cd "$$project" && "$$exe" init >/dev/null ); \
	printf '%s\n' \
		'version: "1.0"' \
		'agents:' \
		'  claude:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'  codex:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'  gemini:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'settings:' \
		'  integrations:' \
		'    invoker:' \
		'      implementation: "mock"' \
		'      options:' \
		'        delay_seconds: 0' \
		'        observation_delay_seconds: 0' \
		'        output_mode: "lorem"' \
		'    ui:' \
		'      implementation: "none"' \
		'      options: {}' \
		'    artifacts:' \
		'      implementation: "filesystem"' \
		'      options:' \
		'        log_cli_output: true' \
		'        allowed_template_paths: []' \
		> "$$project/.orchestrator/config.yml"; \
	( cd "$$project" && "$$exe" validate >/dev/null )

install-smoke: install-smoke-pip install-smoke-uv install-smoke-pipx

install-script-smoke: package-wheelhouse
	@set -eu; \
	smoke_python="$$( $(RUN_PYTHON) -c 'import sys; print(sys.executable)' )"; \
	tmp="$$(mktemp -d)"; \
	trap 'rm -rf "$$tmp"' EXIT; \
	CREWPLANE_VERSION="$(PROJECT_VERSION)" \
	CREWPLANE_INSTALL_FIND_LINKS="$(WHEELHOUSE)" \
	CREWPLANE_INSTALL_NO_INDEX=1 \
	CREWPLANE_INSTALL_PYTHON="$$smoke_python" \
	CREWPLANE_INSTALL_HOME="$$tmp/home" \
	HOME="$$tmp/home" \
		sh install.sh >/dev/null

npm-pack:
	@set -eu; \
	if ! command -v npm >/dev/null 2>&1; then \
		echo "Skipping npm-pack: npm not found."; \
		exit 0; \
	fi; \
	project_version="$(PROJECT_VERSION)"; \
	package_version="$$(node -p 'require("./packaging/npm/package.json").version')"; \
	if [ "$$package_version" != "$$project_version" ]; then \
		echo "packaging/npm/package.json version $$package_version differs from pyproject.toml version $$project_version."; \
		exit 1; \
	fi; \
	mkdir -p "$(NPM_PACK_DIR)"; \
	rm -f "$(NPM_PACK_DIR)"/crewplane-*.tgz; \
	npm pack ./packaging/npm --pack-destination "$(NPM_PACK_DIR)" >/dev/null

npm-smoke: package-wheelhouse npm-pack
	@set -eu; \
	if ! command -v npm >/dev/null 2>&1; then \
		echo "Skipping npm-smoke: npm not found."; \
		exit 0; \
	fi; \
	smoke_python="$$( $(RUN_PYTHON) -c 'import sys; print(sys.executable)' )"; \
	package="$$(ls -t "$(NPM_PACK_DIR)"/crewplane-*.tgz 2>/dev/null | head -n 1)"; \
	test -n "$$package"; \
	tmp="$$(mktemp -d)"; \
	trap 'rm -rf "$$tmp"' EXIT; \
	mkdir -p "$$tmp/home" "$$tmp/npm-cache" "$$tmp/xdg-cache"; \
	HOME="$$tmp/home"; \
	NPM_CONFIG_CACHE="$$tmp/npm-cache"; \
	XDG_CACHE_HOME="$$tmp/xdg-cache"; \
	export HOME NPM_CONFIG_CACHE XDG_CACHE_HOME; \
	CREWPLANE_VERSION="$(PROJECT_VERSION)" \
	CREWPLANE_INSTALL_FIND_LINKS="$(WHEELHOUSE)" \
	CREWPLANE_INSTALL_NO_INDEX=1 \
	CREWPLANE_INSTALL_PYTHON="$$smoke_python" \
		npm install -g "$$package" --prefix "$$tmp/prefix" --foreground-scripts >/dev/null; \
	PATH="$$tmp/prefix/bin:$$PATH"; \
	export PATH; \
	command -v "$(CLI_NAME)" >/dev/null; \
	command -v crewplane >/dev/null; \
	$(CLI_NAME) --help >/dev/null; \
	crewplane --help >/dev/null; \
	project="$$tmp/project"; \
	mkdir -p "$$project"; \
	( cd "$$project" && $(CLI_NAME) init >/dev/null ); \
	printf '%s\n' \
		'version: "1.0"' \
		'agents:' \
		'  claude:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'  codex:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'  gemini:' \
		'    cli_cmd: ["mock"]' \
		'    provider_kind: "generic"' \
		'settings:' \
		'  integrations:' \
		'    invoker:' \
		'      implementation: "mock"' \
		'      options:' \
		'        delay_seconds: 0' \
		'        observation_delay_seconds: 0' \
		'        output_mode: "lorem"' \
		'    ui:' \
		'      implementation: "none"' \
		'      options: {}' \
		'    artifacts:' \
		'      implementation: "filesystem"' \
		'      options:' \
		'        log_cli_output: true' \
		'        allowed_template_paths: []' \
		> "$$project/.orchestrator/config.yml"; \
	( cd "$$project" && $(CLI_NAME) validate >/dev/null )

brew-smoke: package-build
	@set -eu; \
	if ! command -v brew >/dev/null 2>&1; then \
		echo "Skipping brew-smoke: brew not found."; \
		exit 0; \
	fi; \
	if brew list --formula "$(PACKAGE_NAME)" >/dev/null 2>&1; then \
		echo "Skipping brew-smoke: Homebrew formula $(PACKAGE_NAME) is already installed."; \
		exit 0; \
	fi; \
	normalized="$$( $(NORMALIZE_VERSION) )"; \
	sdist="$(CURDIR)/dist/$(PACKAGE_NAME)-$${normalized}.tar.gz"; \
	sha="$$(shasum -a 256 "$$sdist" | awk '{print $$1}')"; \
	tmp="$$(mktemp -d)"; \
	trap 'status=$$?; if brew list --formula "$(PACKAGE_NAME)" >/dev/null 2>&1; then brew uninstall "$(PACKAGE_NAME)" >/dev/null 2>&1 || true; fi; rm -rf "$$tmp"; exit $$status' EXIT; \
	sed \
		-e "1,/url \"https:.*crewplane.*tar.gz\"/s|url \".*\"|url \"file://$$sdist\"|" \
		-e "1,/sha256 \".*\"/s|sha256 \".*\"|sha256 \"$$sha\"|" \
		packaging/homebrew/Formula/crewplane.rb > "$$tmp/crewplane.rb"; \
	brew install --build-from-source "$$tmp/crewplane.rb"; \
	brew test "$(PACKAGE_NAME)"

install-check: package-check install-smoke install-script-smoke npm-pack npm-smoke brew-smoke

release-check: release-version-check release-remote-version-check lint format-check test package-check install-check

release-prereqs:
	@set -eu; \
	if ! command -v npm >/dev/null 2>&1; then \
		echo "npm is required for make release."; \
		exit 1; \
	fi; \
	if ! npm whoami >/dev/null 2>&1; then \
		echo "npm authentication is required before make release."; \
		exit 1; \
	fi

release-pypi: package-check
	@set -eu; \
	normalized="$$( $(NORMALIZE_VERSION) )"; \
	sdist="$(CURDIR)/dist/$(PACKAGE_NAME)-$${normalized}.tar.gz"; \
	wheel="$(CURDIR)/dist/$(PACKAGE_NAME)-$${normalized}-py3-none-any.whl"; \
	test -f "$$sdist"; \
	test -f "$$wheel"; \
	$(RUN_PYTHON) -m twine upload --repository "$(PYPI_REPOSITORY)" $(TWINE_UPLOAD_ARGS) "$$sdist" "$$wheel"

release-npm: npm-pack
	@set -eu; \
	if ! command -v npm >/dev/null 2>&1; then \
		echo "npm is required for release-npm."; \
		exit 1; \
	fi; \
	package="$$(ls -t "$(NPM_PACK_DIR)"/crewplane-*.tgz 2>/dev/null | head -n 1)"; \
	test -n "$$package"; \
	npm publish "$$package" --tag "$(NPM_TAG)" $(NPM_PUBLISH_ARGS)

release: release-confirm release-check changelog-check release-prereqs
	$(MAKE) release-pypi
	$(MAKE) release-npm
	@echo "Released $(PACKAGE_NAME) $(PROJECT_VERSION) to PyPI repository '$(PYPI_REPOSITORY)' and npm tag '$(NPM_TAG)'."

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info .release
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
