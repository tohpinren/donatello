#!/usr/bin/env bash
# Run the bag-primary blocker audit and set-sensitivity pass sequentially.
set -euo pipefail

: "${BIRD_ROOT:?Set BIRD_ROOT to the BIRD benchmark root.}"
: "${VERIEQL_ROOT:?Set VERIEQL_ROOT to the VeriEQL checkout.}"
: "${VERIEQL_PYTHON:?Set VERIEQL_PYTHON to the VeriEQL Python executable.}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

uv run donatello run \
  --config configs/experiments/bird-full-blocker-audit.yaml

scripts/prepare-set-sensitivity.sh

uv run donatello run \
  --config configs/experiments/bird-full-set-sensitivity.yaml
