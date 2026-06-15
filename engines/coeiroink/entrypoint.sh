#!/usr/bin/env bash
set -euo pipefail

ENGINE_ROOT="${COEIROINK_ENGINE_ROOT:-/opt/coeiroink_engine}"
SPEAKER_INFO_DIR="${COEIROINK_SPEAKER_INFO_DIR:-${ENGINE_ROOT}/speaker_info}"
SOURCE="${COEIROINK_SPEAKER_SOURCE:-https://coeiroink.com/download}"
PREFIXES="${COEIROINK_SPEAKER_PREFIXES:-}"
FORCE_INSTALL="${COEIROINK_FORCE_INSTALL:-0}"
INSTALLER_VERSION="2"
MANIFEST="${SPEAKER_INFO_DIR}/.voicevox-discord-coeiroink-manifest.json"

mkdir -p "$SPEAKER_INFO_DIR"

python /usr/local/bin/coeiroink_ensure_openjtalk_dict.py

needs_install=0
if [ "$FORCE_INSTALL" = "1" ] || [ "$FORCE_INSTALL" = "true" ]; then
  needs_install=1
elif ! find "$SPEAKER_INFO_DIR" -mindepth 2 -name metas.json -type f | grep -q .; then
  needs_install=1
elif ! python /usr/local/bin/coeiroink_manifest.py check \
    --manifest "$MANIFEST" \
    --source "$SOURCE" \
    --prefixes "$PREFIXES" \
    --installer-version "$INSTALLER_VERSION"; then
  needs_install=1
fi

if [ "$needs_install" = "1" ]; then
  echo "Installing COEIROINK speaker data into ${SPEAKER_INFO_DIR}"
  python /usr/local/bin/coeiroink_install_speakers.py \
    --source "$SOURCE" \
    --engine-root "$ENGINE_ROOT" \
    --prefixes "$PREFIXES" \
    --clean
  python /usr/local/bin/coeiroink_manifest.py write \
    --manifest "$MANIFEST" \
    --source "$SOURCE" \
    --prefixes "$PREFIXES" \
    --installer-version "$INSTALLER_VERSION"
else
  echo "COEIROINK speaker data already exists in ${SPEAKER_INFO_DIR}; skipping download"
fi

exec "$@"
