#!/bin/sh
set -eu

PACKAGE_NAME="crewplane"
CREWPLANE_VERSION="${CREWPLANE_VERSION:-0.1.0-alpha.2}"

fail() {
    printf '%s\n' "error: $*" >&2
    exit 1
}

info() {
    printf '%s\n' "$*"
}

detect_supported_platform() {
    kernel_name="$(uname -s 2>/dev/null || printf unknown)"
    case "$kernel_name" in
        Darwin)
            return 0
            ;;
        Linux)
            if [ -r /proc/version ] && grep -qi microsoft /proc/version; then
                return 0
            fi
            if [ -r /etc/os-release ] && grep -Eq '^(ID|ID_LIKE)=.*(ubuntu|debian)' /etc/os-release; then
                return 0
            fi
            fail "unsupported Linux distribution. Use macOS or WSL/Ubuntu-style Linux."
            ;;
        MINGW*|MSYS*|CYGWIN*)
            fail "native Windows is not supported by this installer. Use WSL."
            ;;
        *)
            fail "unsupported platform: $kernel_name"
            ;;
    esac
}

set_install_home() {
    if [ -n "${CREWPLANE_INSTALL_HOME:-}" ]; then
        mkdir -p "$CREWPLANE_INSTALL_HOME"
        HOME="$CREWPLANE_INSTALL_HOME"
        export HOME
    fi
}

find_uv() {
    if [ -n "${CREWPLANE_UV_BIN:-}" ]; then
        [ -x "$CREWPLANE_UV_BIN" ] || fail "CREWPLANE_UV_BIN is not executable: $CREWPLANE_UV_BIN"
        printf '%s\n' "$CREWPLANE_UV_BIN"
        return 0
    fi
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    return 1
}

install_uv() {
    info "uv was not found; installing uv for the current user without sudo."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        fail "curl or wget is required to install uv automatically"
    fi

    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    fail "uv installation completed but uv was not found under $HOME/.local/bin or $HOME/.cargo/bin"
}

install_crewplane() {
    uv_bin="$1"
    package_spec="${PACKAGE_NAME}==${CREWPLANE_VERSION}"
    find_links="${CREWPLANE_INSTALL_FIND_LINKS:-}"
    no_index="${CREWPLANE_INSTALL_NO_INDEX:-}"
    python="${CREWPLANE_INSTALL_PYTHON:-}"

    if [ -n "$python" ] && [ -n "$find_links" ] && [ "$no_index" != "0" ]; then
        "$uv_bin" tool install --force --python "$python" --find-links "$find_links" --no-index "$package_spec"
    elif [ -n "$python" ] && [ -n "$find_links" ]; then
        "$uv_bin" tool install --force --python "$python" --find-links "$find_links" "$package_spec"
    elif [ -n "$python" ]; then
        "$uv_bin" tool install --force --python "$python" "$package_spec"
    elif [ -n "$find_links" ] && [ "$no_index" != "0" ]; then
        "$uv_bin" tool install --force --find-links "$find_links" --no-index "$package_spec"
    elif [ -n "$find_links" ]; then
        "$uv_bin" tool install --force --find-links "$find_links" "$package_spec"
    else
        "$uv_bin" tool install --force "$package_spec"
    fi
}

path_remediation() {
    tool_bin="$1"
    shell_name="$(basename "${SHELL:-sh}")"
    info ""
    info "Add the uv tool directory to PATH if '${PACKAGE_NAME}' is not found by your shell:"
    case "$shell_name" in
        zsh)
            info "  echo 'export PATH=\"$tool_bin:\$PATH\"' >> ~/.zshrc"
            info "  export PATH=\"$tool_bin:\$PATH\""
            ;;
        bash)
            info "  echo 'export PATH=\"$tool_bin:\$PATH\"' >> ~/.bashrc"
            info "  export PATH=\"$tool_bin:\$PATH\""
            ;;
        fish)
            info "  fish_add_path \"$tool_bin\""
            ;;
        *)
            info "  export PATH=\"$tool_bin:\$PATH\""
            ;;
    esac
}

verify_cli() {
    uv_bin="$1"
    tool_bin="$("$uv_bin" tool dir --bin)"
    cli_path="$tool_bin/$PACKAGE_NAME"

    if [ -x "$cli_path" ]; then
        "$cli_path" --help >/dev/null
    elif command -v "$PACKAGE_NAME" >/dev/null 2>&1; then
        "$PACKAGE_NAME" --help >/dev/null
    else
        path_remediation "$tool_bin"
        fail "'$PACKAGE_NAME' was installed but is not on PATH"
    fi

    if ! command -v "$PACKAGE_NAME" >/dev/null 2>&1; then
        path_remediation "$tool_bin"
    fi
}

print_provider_notes() {
    info ""
    info "First run:"
    info "  Run '${PACKAGE_NAME} init', '${PACKAGE_NAME} validate', then '${PACKAGE_NAME} run --no-live'."
    info "  The generated first run uses deterministic mock execution; provider CLIs are not required."
    info "Real provider setup:"
    info "  Install and authenticate provider CLIs separately, such as claude, codex, gemini, copilot, or kilo."
    info "  ${PACKAGE_NAME} does not install provider CLIs, manage provider credentials, or sandbox provider CLI execution."
}

print_uninstall_notes() {
    info ""
    info "Uninstall:"
    info "  uv tool uninstall ${PACKAGE_NAME}"
}

main() {
    detect_supported_platform
    set_install_home
    if uv_bin="$(find_uv)"; then
        :
    else
        uv_bin="$(install_uv)"
    fi

    install_crewplane "$uv_bin"
    verify_cli "$uv_bin"
    info "Installed ${PACKAGE_NAME} ${CREWPLANE_VERSION}. Run '${PACKAGE_NAME} --help' to start."
    print_provider_notes
    print_uninstall_notes
}

main "$@"
