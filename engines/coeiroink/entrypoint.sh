#!/usr/bin/env bash
set -euo pipefail

ENGINE_ROOT="${COEIROINK_ENGINE_ROOT:-/opt/coeiroink_engine}"
CACHE_ROOT="${COEIROINK_CACHE_ROOT:-/opt/coeiroink_cache}"
ENGINE_REF="${COEIROINK_ENGINE_REF:-c-1.6.0+v-0.12.3}"
SOURCE="${COEIROINK_SPEAKER_SOURCE:-https://coeiroink.com/download}"
PREFIXES="${COEIROINK_SPEAKER_PREFIXES:-}"
FORCE_INSTALL="${COEIROINK_FORCE_INSTALL:-0}"
INSTALLER_VERSION="3"

mkdir -p "${CACHE_ROOT}/releases"
python /usr/local/bin/coeiroink_ensure_openjtalk_dict.py

cache_key="$(python /usr/local/bin/coeiroink_manifest.py cache-key \
  --engine-ref "$ENGINE_REF" \
  --source "$SOURCE" \
  --prefixes "$PREFIXES" \
  --installer-version "$INSTALLER_VERSION")"
if [ "$FORCE_INSTALL" = "1" ] || [ "$FORCE_INSTALL" = "true" ]; then
  cache_key="${cache_key}-force-$(date +%s)-$$"
fi

release_dir="${CACHE_ROOT}/releases/${cache_key}"
manifest="${release_dir}/.voicevox-discord-coeiroink-manifest.json"
mkdir -p "$release_dir"

if python /usr/local/bin/coeiroink_manifest.py check \
    --manifest "$manifest" \
    --speaker-info-dir "$release_dir" \
    --engine-ref "$ENGINE_REF" \
    --source "$SOURCE" \
    --prefixes "$PREFIXES" \
    --installer-version "$INSTALLER_VERSION"; then
  echo "COEIROINK speaker cache is complete: ${release_dir}"
else
  echo "Installing COEIROINK speakers into inactive cache: ${release_dir}"
  python /usr/local/bin/coeiroink_install_speakers.py \
    --source "$SOURCE" \
    --engine-root "$ENGINE_ROOT" \
    --speaker-info-dir "$release_dir" \
    --prefixes "$PREFIXES"
  python /usr/local/bin/coeiroink_manifest.py write \
    --manifest "$manifest" \
    --speaker-info-dir "$release_dir" \
    --engine-ref "$ENGINE_REF" \
    --source "$SOURCE" \
    --prefixes "$PREFIXES" \
    --installer-version "$INSTALLER_VERSION"
fi

# The active symlink changes only after the complete release passed validation.
python /usr/local/bin/coeiroink_manifest.py activate \
  --cache-root "$CACHE_ROOT" \
  --release-dir "$release_dir" \
  --engine-root "$ENGINE_ROOT"

exec "$@"
