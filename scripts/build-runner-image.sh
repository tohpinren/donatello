#!/usr/bin/env bash
# Build the Donatello runner image from the local repo plus sibling VeriEQL/SQLDriller checkouts.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_TOOLS_ROOT="$(cd "${REPO_ROOT}/.." && pwd)/tools"

VERIEQL_SOURCE="${VERIEQL_SOURCE:-${DEFAULT_TOOLS_ROOT}/VeriEQL}"
SQLDRILLER_SOURCE="${SQLDRILLER_SOURCE:-${DEFAULT_TOOLS_ROOT}/SQLDriller}"
DONATELLO_RUNNER_IMAGE="${DONATELLO_RUNNER_IMAGE:-donatello-runner:latest}"

if [[ ! -d "${VERIEQL_SOURCE}" ]]; then
  echo "VeriEQL checkout not found: ${VERIEQL_SOURCE}" >&2
  exit 1
fi

if [[ ! -d "${SQLDRILLER_SOURCE}" ]]; then
  echo "SQLDriller checkout not found: ${SQLDRILLER_SOURCE}" >&2
  exit 1
fi

export DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}"

BUILD_CONTEXT="$(mktemp -d)"
trap 'rm -rf "${BUILD_CONTEXT}"' EXIT

mkdir -p "${BUILD_CONTEXT}/donatello" "${BUILD_CONTEXT}/tools/VeriEQL" "${BUILD_CONTEXT}/tools/SQLDriller"

tar -C "${REPO_ROOT}" \
  --exclude=.git \
  --exclude=.venv \
  --exclude=.pytest_cache \
  --exclude=__pycache__ \
  --exclude='outputs/*' \
  --exclude='data/*' \
  -cf - . | tar -C "${BUILD_CONTEXT}/donatello" -xf -

tar -C "${VERIEQL_SOURCE}" \
  --exclude=.git \
  --exclude=__pycache__ \
  -cf - . | tar -C "${BUILD_CONTEXT}/tools/VeriEQL" -xf -

tar -C "${SQLDRILLER_SOURCE}" \
  --exclude=.git \
  --exclude=__pycache__ \
  -cf - . | tar -C "${BUILD_CONTEXT}/tools/SQLDriller" -xf -

docker build \
  -f "${REPO_ROOT}/Dockerfile" \
  -t "${DONATELLO_RUNNER_IMAGE}" \
  "${BUILD_CONTEXT}"
