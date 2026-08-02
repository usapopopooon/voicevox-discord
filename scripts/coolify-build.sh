#!/usr/bin/env bash
set -euo pipefail

# Coolify's previous bot-only command left optional engine images unavailable.
# Build all locally defined images so a deployment can always start every engine.
docker compose build coeiroink sharevox voicevox-discord
