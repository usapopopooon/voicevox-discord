#!/usr/bin/env bash
set -euo pipefail

force_args=()
if [ "${SHAREVOX_FORCE_INSTALL:-0}" = "1" ] || [ "${SHAREVOX_FORCE_INSTALL:-0}" = "true" ]; then
  force_args=(--force)
fi

python /usr/local/bin/sharevox_install_assets.py "${force_args[@]}"

exec "$@"
