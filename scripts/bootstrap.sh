#!/usr/bin/env bash
# Bootstrap the local Python environment with the pinned uv project state.
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/." >&2
  exit 1
fi

uv sync
