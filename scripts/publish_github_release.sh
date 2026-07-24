#!/usr/bin/env bash
set -euo pipefail

RELEASE_AUTOMATION_MARKER='<!-- crewplane-release:v1 -->'

sha256_file() {
  local path="$1"
  local digest
  if command -v sha256sum >/dev/null 2>&1; then
    read -r digest _ < <(sha256sum -- "$path")
  elif command -v shasum >/dev/null 2>&1; then
    read -r digest _ < <(shasum -a 256 -- "$path")
  else
    echo "sha256sum or shasum is required to verify release assets." >&2
    return 1
  fi
  if [[ ! "$digest" =~ ^[a-f0-9]{64}$ ]]; then
    echo "Could not calculate a valid SHA-256 digest for $path." >&2
    return 1
  fi
  printf '%s\n' "$digest"
}

validate_boolean() {
  local name="$1"
  local value="$2"
  if [[ "$value" != "true" && "$value" != "false" ]]; then
    echo "$name must be 'true' or 'false'." >&2
    return 1
  fi
}

build_expected_assets() {
  local records_file="$1"
  local names_file="$2"
  local unsorted_records="$3"
  local unsorted_names="$4"
  shift 4
  local release_artifacts=("$@")

  : >"$unsorted_records"
  : >"$unsorted_names"
  local artifact
  for artifact in "${release_artifacts[@]}"; do
    if [[ ! -f "$artifact" || -L "$artifact" ]]; then
      echo "Release artifact must be a regular, non-symlink file: $artifact" >&2
      return 1
    fi
    local name="${artifact##*/}"
    if printf '%s' "$name" | LC_ALL=C grep -q '[[:cntrl:]]'; then
      echo "Release asset names may not contain control characters: $name" >&2
      return 1
    fi
    local size
    size="$(wc -c <"$artifact")"
    size="${size//[[:space:]]/}"
    local digest
    digest="$(sha256_file "$artifact")"
    printf '%s\t%s\tsha256:%s\n' \
      "$name" "$size" "$digest" >>"$unsorted_records"
    printf '%s\n' "$name" >>"$unsorted_names"
  done
  LC_ALL=C sort "$unsorted_records" >"$records_file"
  LC_ALL=C sort "$unsorted_names" >"$names_file"

  if [[ "$(wc -l <"$names_file")" -ne "${#release_artifacts[@]}" ]]; then
    echo "Could not account for every local release artifact." >&2
    return 1
  fi
}

query_release_snapshot() {
  local repository="$1"
  local tag_name="$2"
  local snapshot_file="$3"
  local owner="${repository%%/*}"
  local name="${repository#*/}"
  if [[ ! "$repository" =~ ^[^/]+/[^/]+$ ]]; then
    echo "GH_REPO must use the owner/repository form." >&2
    return 1
  fi

  if ! gh api graphql \
    -f owner="$owner" \
    -f name="$name" \
    -f tag="$tag_name" \
    -f query='query($owner: String!, $name: String!, $tag: String!) {
      repository(owner: $owner, name: $name) {
        release(tagName: $tag) {
          id
          name
          description
          isDraft
          isPrerelease
          isLatest
          releaseAssets(first: 100) {
            totalCount
            nodes { name size digest }
          }
        }
      }
    }' \
    --jq '
      .data.repository as $repository |
      if $repository == null then
        error("repository not found")
      elif $repository.release == null then
        ["release", "absent"] | @tsv
      else
        $repository.release as $release |
        if ($release.name | type) != "string" or
           ($release.description | type) != "string" then
          error("release title or description is missing")
        else
          (["release", "present", $release.id, $release.name,
            (($release.description |
              startswith("'"$RELEASE_AUTOMATION_MARKER"'")) | tostring),
            ($release.isDraft | tostring),
            ($release.isPrerelease | tostring),
            ($release.isLatest | tostring),
            ($release.releaseAssets.totalCount | tostring),
            ($release.releaseAssets.nodes | length | tostring)] | @tsv),
          ($release.releaseAssets.nodes[] |
            ["asset", .name, (.size | tostring), (.digest // "")] | @tsv)
        end
      end
    ' >"$snapshot_file"; then
    echo "Could not query GitHub Release $tag_name; refusing to mutate it." >&2
    return 1
  fi
}

load_release_snapshot() {
  local snapshot_file="$1"
  local assets_file="$2"
  local unsorted_assets_file="$3"
  : >"$unsorted_assets_file"

  local first_line
  if ! IFS= read -r first_line <"$snapshot_file"; then
    echo "GitHub returned an empty release response." >&2
    return 1
  fi
  if [[ "$first_line" == $'release\tabsent' ]]; then
    if [[ "$(wc -l <"$snapshot_file")" -ne 1 ]]; then
      echo "GitHub returned malformed metadata for an absent release." >&2
      return 1
    fi
    release_exists=false
    release_title=""
    release_has_automation_marker=""
    release_is_draft=""
    release_is_prerelease=""
    release_is_latest=""
    : >"$assets_file"
    return 0
  fi

  local record
  local presence
  local release_id
  local total_count
  local returned_count
  local extra
  IFS=$'\t' read -r record presence release_id release_title \
    release_has_automation_marker release_is_draft release_is_prerelease \
    release_is_latest total_count returned_count extra <<<"$first_line"
  if [[ "$record" != "release" || "$presence" != "present" || -z "$release_id" || -n "$extra" ]]; then
    echo "GitHub returned malformed release metadata." >&2
    return 1
  fi
  validate_boolean \
    "GitHub release automation marker state" \
    "$release_has_automation_marker"
  validate_boolean "GitHub draft state" "$release_is_draft"
  validate_boolean "GitHub prerelease state" "$release_is_prerelease"
  validate_boolean "GitHub Latest state" "$release_is_latest"
  if [[ ! "$total_count" =~ ^[0-9]+$ || ! "$returned_count" =~ ^[0-9]+$ ]]; then
    echo "GitHub returned invalid release asset counts." >&2
    return 1
  fi

  local parsed_count=0
  while IFS=$'\t' read -r record asset_name asset_size asset_digest extra; do
    if [[ "$record" != "asset" || -z "$asset_name" || -n "$extra" ]]; then
      echo "GitHub returned malformed release asset metadata." >&2
      return 1
    fi
    if [[ ! "$asset_size" =~ ^[0-9]+$ ]]; then
      echo "GitHub returned incomplete size or digest metadata for $asset_name." >&2
      return 1
    fi
    if [[ ! "$asset_digest" =~ ^sha256:[a-f0-9]{64}$ ]]; then
      if [[ "$release_is_draft" != "true" || -n "$asset_digest" ]]; then
        echo "GitHub returned incomplete size or digest metadata for $asset_name." >&2
        return 1
      fi
    fi
    printf '%s\t%s\t%s\n' \
      "$asset_name" "$asset_size" "$asset_digest" \
      >>"$unsorted_assets_file"
    parsed_count=$((parsed_count + 1))
  done < <(tail -n +2 "$snapshot_file")

  if [[ "$total_count" -ne "$returned_count" || "$returned_count" -ne "$parsed_count" ]]; then
    echo "GitHub release asset query was truncated or internally inconsistent." >&2
    return 1
  fi
  LC_ALL=C sort "$unsorted_assets_file" >"$assets_file"
  release_exists=true
}

refresh_release_state() {
  query_release_snapshot "$repository" "$tag_name" "$snapshot_file"
  load_release_snapshot "$snapshot_file" "$release_assets_file" \
    "$unsorted_release_assets_file"
}

verify_loaded_assets() {
  local label="$1"
  if ! cmp -s "$expected_assets_file" "$release_assets_file"; then
    echo "$label assets do not match the verified dist artifacts:" >&2
    diff -u "$expected_assets_file" "$release_assets_file" >&2 || true
    return 1
  fi
}

verify_loaded_release_metadata() {
  local label="$1"
  if [[ "$release_title" != "$tag_name" ]]; then
    echo "$label title does not match the release tag." >&2
    return 1
  fi
  if [[ "$release_has_automation_marker" != "true" ]]; then
    echo "$label notes are missing the release automation marker." >&2
    return 1
  fi
}

verify_loaded_published_release() {
  local label="$1"
  if [[ "$release_exists" != "true" || "$release_is_draft" != "false" ]]; then
    echo "$label is missing or still a draft." >&2
    return 1
  fi
  verify_loaded_release_metadata "$label"
  verify_loaded_assets "$label"
  if [[ "$release_is_prerelease" != "$is_prerelease" ]]; then
    echo "$label prerelease state does not match the verified version." >&2
    return 1
  fi
  if [[ "$release_is_latest" != "$is_latest" ]]; then
    echo "$label Latest state does not match the verified release plan." >&2
    return 1
  fi
}

verify_loaded_draft_release() {
  local label="$1"
  if [[ "$release_exists" != "true" || "$release_is_draft" != "true" ]]; then
    echo "$label is missing or is no longer a draft." >&2
    return 1
  fi
  verify_loaded_release_metadata "$label"
  verify_loaded_assets "$label"
}

refresh_verified_release_plan() {
  local plan_file="$1"
  if ! uv run --locked --extra dev \
    python scripts/release.py github-release-plan \
    --expected-tag "$tag_name" >"$plan_file"; then
    echo "Could not verify the release plan; refusing to mutate GitHub." >&2
    return 1
  fi

  local line_count
  line_count="$(wc -l <"$plan_file")"
  line_count="${line_count//[[:space:]]/}"
  local prerelease_line
  local latest_line
  local notes_start_tag_line
  IFS= read -r prerelease_line <"$plan_file"
  latest_line="$(sed -n '2p' "$plan_file")"
  notes_start_tag_line="$(sed -n '3p' "$plan_file")"
  if [[ "$line_count" != "3" \
    || ! "$prerelease_line" =~ ^prerelease=(true|false)$ \
    || ! "$latest_line" =~ ^latest=(true|false)$ \
    || ! "$notes_start_tag_line" =~ ^notes_start_tag=(v[0-9A-Za-z._!-]+)?$ ]]; then
    echo "The verified release plan returned malformed output." >&2
    return 1
  fi
  is_prerelease="${prerelease_line#prerelease=}"
  is_latest="${latest_line#latest=}"
  notes_start_tag="${notes_start_tag_line#notes_start_tag=}"
  if [[ "$is_prerelease" == "true" && "$is_latest" == "true" ]]; then
    echo "The verified release plan marked a prerelease as GitHub Latest." >&2
    return 1
  fi
}

publish_github_release() (
  local dist_dir="$1"
  local tag_name="${TAG_NAME:?TAG_NAME is required}"
  local repository="${GH_REPO:?GH_REPO is required}"

  shopt -s nullglob dotglob
  local release_artifacts=("$dist_dir"/*)
  if [[ "${#release_artifacts[@]}" -eq 0 ]]; then
    echo "No verified release artifacts were found in $dist_dir." >&2
    return 1
  fi

  local temp_dir
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  local expected_assets_file="$temp_dir/expected-assets.tsv"
  local expected_names_file="$temp_dir/expected-names.txt"
  local unsorted_expected_assets_file="$temp_dir/expected-assets.unsorted.tsv"
  local unsorted_expected_names_file="$temp_dir/expected-names.unsorted.txt"
  local snapshot_file="$temp_dir/release-snapshot.tsv"
  local release_assets_file="$temp_dir/release-assets.tsv"
  local unsorted_release_assets_file="$temp_dir/release-assets.unsorted.tsv"
  local plan_file="$temp_dir/release-plan.txt"
  build_expected_assets \
    "$expected_assets_file" \
    "$expected_names_file" \
    "$unsorted_expected_assets_file" \
    "$unsorted_expected_names_file" \
    "${release_artifacts[@]}"

  local is_prerelease
  local is_latest
  local notes_start_tag
  refresh_verified_release_plan "$plan_file"
  local release_exists
  local release_title
  local release_has_automation_marker
  local release_is_draft
  local release_is_prerelease
  local release_is_latest
  refresh_release_state

  if [[ "$release_exists" == "true" && "$release_is_draft" == "false" ]]; then
    verify_loaded_published_release "Published release"
    echo "Verified existing published GitHub Release $tag_name; leaving it unchanged."
    return 0
  fi

  if [[ "$release_exists" == "true" ]]; then
    verify_loaded_release_metadata "Existing draft"
    local release_names_file="$temp_dir/release-names.txt"
    cut -f1 "$release_assets_file" >"$release_names_file"
    local unexpected_assets
    unexpected_assets="$(comm -13 "$expected_names_file" "$release_names_file")"
    if [[ -n "$unexpected_assets" ]]; then
      echo "Refusing to publish a draft with unexpected assets:" >&2
      printf '%s\n' "$unexpected_assets" >&2
      return 1
    fi

    gh release upload "$tag_name" "${release_artifacts[@]}" \
      --repo "$repository" \
      --clobber
    refresh_release_state
    verify_loaded_draft_release "Draft"
  else
    local release_notes_flags=()
    if [[ -n "$notes_start_tag" ]]; then
      release_notes_flags=(--notes-start-tag "$notes_start_tag")
    fi
    gh release create "$tag_name" "${release_artifacts[@]}" \
      --repo "$repository" \
      --draft \
      --verify-tag \
      --generate-notes \
      --notes "$RELEASE_AUTOMATION_MARKER" \
      "${release_notes_flags[@]}" \
      --title "$tag_name" \
      --prerelease=false \
      --latest=false
    refresh_release_state
    verify_loaded_draft_release "New draft"
  fi

  refresh_verified_release_plan "$plan_file"
  refresh_release_state
  verify_loaded_draft_release "Final draft"
  local release_flags=(--prerelease=false --latest=false)
  if [[ "$is_prerelease" == "true" ]]; then
    release_flags=(--prerelease --latest=false)
  elif [[ "$is_latest" == "true" ]]; then
    release_flags=(--prerelease=false --latest)
  fi

  gh release edit "$tag_name" \
    --repo "$repository" \
    --tag "$tag_name" \
    --draft=false \
    --verify-tag \
    "${release_flags[@]}"
  refresh_release_state
  verify_loaded_published_release "Published release"
  echo "Published GitHub Release $tag_name."
)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  if [[ "$#" -ne 1 ]]; then
    echo "Usage: $0 DIST_DIR" >&2
    exit 2
  fi
  publish_github_release "$1"
fi
