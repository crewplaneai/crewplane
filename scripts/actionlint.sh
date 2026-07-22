#!/usr/bin/env bash
set -euo pipefail

readonly ACTIONLINT_VERSION="1.7.9"
# actionlint 1.7.9 predates GitHub's supported concurrency.queue property.
readonly ACTIONLINT_QUEUE_IGNORE='unexpected key "queue" for "concurrency" section'

actionlint_release_metadata() {
  case "$1-$2" in
    Darwin-x86_64)
      printf '%s %s\n' \
        "actionlint_${ACTIONLINT_VERSION}_darwin_amd64.tar.gz" \
        "f89a910e90e536f60df7c504160247db01dd67cab6f08c064c1c397b76c91a79"
      ;;
    Darwin-arm64)
      printf '%s %s\n' \
        "actionlint_${ACTIONLINT_VERSION}_darwin_arm64.tar.gz" \
        "855e49e823fc68c6371fd6967e359cde11912d8d44fed343283c8e6e943bd789"
      ;;
    Linux-x86_64 | Linux-amd64)
      printf '%s %s\n' \
        "actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" \
        "233b280d05e100837f4af1433c7b40a5dcb306e3aa68fb4f17f8a7f45a7df7b4"
      ;;
    Linux-aarch64 | Linux-arm64)
      printf '%s %s\n' \
        "actionlint_${ACTIONLINT_VERSION}_linux_arm64.tar.gz" \
        "6b82a3b8c808bf1bcd39a95aced22fc1a026eef08ede410f81e274af8deadbbc"
      ;;
    *)
      echo "actionlint bootstrap does not support platform $1-$2." >&2
      return 1
      ;;
  esac
}

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "actionlint bootstrap requires '$1'." >&2
    return 1
  fi
}

verify_actionlint_checksum() {
  local archive_path="$1"
  local expected_checksum="$2"
  local checksum_output

  if command -v sha256sum >/dev/null 2>&1; then
    checksum_output="$(sha256sum "$archive_path")"
  elif command -v shasum >/dev/null 2>&1; then
    checksum_output="$(shasum -a 256 "$archive_path")"
  else
    echo "actionlint bootstrap requires 'sha256sum' or 'shasum'." >&2
    return 1
  fi

  local actual_checksum="${checksum_output%% *}"
  if [[ "$actual_checksum" != "$expected_checksum" ]]; then
    echo "actionlint archive checksum mismatch." >&2
    return 1
  fi
}

main() {
  if command -v actionlint >/dev/null 2>&1 \
    && [[ "$(actionlint -version 2>/dev/null | head -n 1)" == "$ACTIONLINT_VERSION" ]]; then
    exec actionlint -color -ignore "$ACTIONLINT_QUEUE_IGNORE" "$@"
  fi

  local release_metadata
  release_metadata="$(actionlint_release_metadata "$(uname -s)" "$(uname -m)")"
  local archive
  local expected_checksum
  read -r archive expected_checksum <<<"$release_metadata"

  require_tool curl
  require_tool tar
  require_tool mktemp

  local url="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/${archive}"
  local temp_dir
  (
    temp_dir="$(mktemp -d)"
    trap 'rm -rf "$temp_dir"' EXIT

    curl \
      --connect-timeout 10 \
      --fail \
      --location \
      --max-time 120 \
      --silent \
      --show-error \
      "$url" \
      --output "$temp_dir/$archive"
    verify_actionlint_checksum "$temp_dir/$archive" "$expected_checksum"
    tar -xzf "$temp_dir/$archive" -C "$temp_dir" actionlint
    "$temp_dir/actionlint" -color -ignore "$ACTIONLINT_QUEUE_IGNORE" "$@"
  )
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
