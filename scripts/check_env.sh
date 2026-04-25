#!/usr/bin/env bash
# Run the repository's lightweight environment health check.
set -euo pipefail

uv run sqlmt check-env
