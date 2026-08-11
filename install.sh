#!/bin/sh
set -eu

PACKAGE_NAME="crewplane"
CREWPLANE_VERSION="${CREWPLANE_VERSION:-}"
# BEGIN GENERATED UV BOOTSTRAP METADATA
UV_VERSION="0.12.3"
UV_RELEASE_BASE_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"

uv_archive_details() {
    uv_platform="$(uname -s 2>/dev/null || printf unknown):$(uname -m 2>/dev/null || printf unknown)"
    case "$uv_platform" in
        Linux:*)
            uv_libc="gnu"
            if command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; then
                uv_libc="musl"
            fi
            uv_platform="${uv_platform}:${uv_libc}"
            ;;
    esac
    case "$uv_platform" in
        Darwin:arm64)
            printf '%s|%s\n' \
                "aarch64-apple-darwin" \
                "546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843"
            ;;
        Darwin:x86_64)
            printf '%s|%s\n' \
                "x86_64-apple-darwin" \
                "4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b"
            ;;
        Linux:aarch64:gnu|Linux:arm64:gnu)
            printf '%s|%s\n' \
                "aarch64-unknown-linux-gnu" \
                "bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2"
            ;;
        Linux:aarch64:musl|Linux:arm64:musl)
            printf '%s|%s\n' \
                "aarch64-unknown-linux-musl" \
                "fa513fca1eb2913334c944fe9adbdd410274a1cbe8dd05d03699a9eb85311d4e"
            ;;
        Linux:x86_64:gnu|Linux:amd64:gnu)
            printf '%s|%s\n' \
                "x86_64-unknown-linux-gnu" \
                "600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101"
            ;;
        Linux:x86_64:musl|Linux:amd64:musl)
            printf '%s|%s\n' \
                "x86_64-unknown-linux-musl" \
                "0643b9fb8c9fb27458e709ce6ff939695013c41975ff7b02d3f3b138d8d4bdb3"
            ;;
        *)
            fail "unsupported platform for automatic uv installation: $uv_platform"
            ;;
    esac
}
# END GENERATED UV BOOTSTRAP METADATA

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

download_file() {
    download_url="$1"
    download_path="$2"
    if command -v curl >/dev/null 2>&1; then
        curl --proto '=https' --tlsv1.2 -LsSf -o "$download_path" "$download_url"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$download_path" "$download_url"
    else
        fail "curl or wget is required to install uv automatically"
    fi
}

file_sha256() {
    checksum_path="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$checksum_path" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$checksum_path" | awk '{print $1}'
    else
        fail "sha256sum or shasum is required to verify the uv download"
    fi
}

install_uv() {
    info "uv was not found; installing uv for the current user without sudo." >&2
    uv_details="$(uv_archive_details)"
    uv_target="${uv_details%%|*}"
    uv_sha256="${uv_details#*|}"
    uv_archive="uv-${uv_target}.tar.gz"
    uv_url="${UV_RELEASE_BASE_URL}/${uv_archive}"
    tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/crewplane-uv.XXXXXX")"
    cleanup_uv_install() {
        rm -rf "$tmp_dir"
    }
    trap cleanup_uv_install 0
    trap 'exit 1' 1 2 15

    archive_path="$tmp_dir/$uv_archive"
    if ! download_file "$uv_url" "$archive_path"; then
        fail "failed to download uv ${UV_VERSION}"
    fi
    actual_sha256="$(file_sha256 "$archive_path")"
    if [ "$actual_sha256" != "$uv_sha256" ]; then
        fail "uv archive checksum mismatch"
    fi
    if ! tar -xzf "$archive_path" -C "$tmp_dir"; then
        fail "failed to extract uv ${UV_VERSION}"
    fi

    archive_dir="$tmp_dir/uv-$uv_target"
    [ -f "$archive_dir/uv" ] || fail "uv archive did not contain the uv executable"
    [ -f "$archive_dir/uvx" ] || fail "uv archive did not contain the uvx executable"
    install_dir="$HOME/.local/bin"
    mkdir -p "$install_dir"
    cp "$archive_dir/uv" "$install_dir/uv"
    cp "$archive_dir/uvx" "$install_dir/uvx"
    chmod 0755 "$install_dir/uv" "$install_dir/uvx"

    cleanup_uv_install
    trap - 0 1 2 15
    printf '%s\n' "$install_dir/uv"
}

install_crewplane() {
    uv_bin="$1"
    package_spec="$PACKAGE_NAME"
    find_links="${CREWPLANE_INSTALL_FIND_LINKS:-}"
    no_index="${CREWPLANE_INSTALL_NO_INDEX:-}"
    python="${CREWPLANE_INSTALL_PYTHON:-}"

    if [ -n "$CREWPLANE_VERSION" ]; then
        package_spec="${PACKAGE_NAME}==${CREWPLANE_VERSION}"
    fi

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
    info "  Run '${PACKAGE_NAME} init', '${PACKAGE_NAME} validate', then '${PACKAGE_NAME} run'."
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
    info "Installed ${PACKAGE_NAME}. Run '${PACKAGE_NAME} --help' to start."
    print_provider_notes
    print_uninstall_notes
}

main "$@"
