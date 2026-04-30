#!/usr/bin/env bash
# Build and launch the SQLDriller-compatible VeriEQL sidecar plus its companion MySQL container.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_TOOLS_ROOT="$(cd "${REPO_ROOT}/.." && pwd)/tools"

VERIEQL_SOURCE="${VERIEQL_SOURCE:-${DEFAULT_TOOLS_ROOT}/VeriEQL}"
SQLDRILLER_SOURCE="${SQLDRILLER_SOURCE:-${DEFAULT_TOOLS_ROOT}/SQLDriller}"
VERIEQL_SIDECAR_IMAGE="${VERIEQL_SIDECAR_IMAGE:-sqlmt-verieql-sidecar:latest}"
VERIEQL_CONTAINER_NAME="${VERIEQL_CONTAINER_NAME:-verieql}"
VERIEQL_MYSQL_CONTAINER_NAME="${VERIEQL_MYSQL_CONTAINER_NAME:-verieql-mysql}"
VERIEQL_NETWORK="${VERIEQL_NETWORK:-sqlmt-verieql}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-VeriEQL@2024}"

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

mkdir -p "${BUILD_CONTEXT}/tools/VeriEQL" "${BUILD_CONTEXT}/tools/SQLDriller"

tar -C "${VERIEQL_SOURCE}" \
  --exclude=.git \
  --exclude=__pycache__ \
  -cf - . | tar -C "${BUILD_CONTEXT}/tools/VeriEQL" -xf -

tar -C "${SQLDRILLER_SOURCE}" \
  --exclude=.git \
  --exclude=__pycache__ \
  -cf - . | tar -C "${BUILD_CONTEXT}/tools/SQLDriller" -xf -

docker build \
  -f "${REPO_ROOT}/docker/verieql-sidecar/Dockerfile" \
  -t "${VERIEQL_SIDECAR_IMAGE}" \
  "${BUILD_CONTEXT}"

docker network inspect "${VERIEQL_NETWORK}" >/dev/null 2>&1 \
  || docker network create "${VERIEQL_NETWORK}" >/dev/null

docker rm -f "${VERIEQL_CONTAINER_NAME}" >/dev/null 2>&1 || true
docker rm -f "${VERIEQL_MYSQL_CONTAINER_NAME}" >/dev/null 2>&1 || true

docker run -d \
  --name "${VERIEQL_MYSQL_CONTAINER_NAME}" \
  --network "${VERIEQL_NETWORK}" \
  --restart unless-stopped \
  -e "MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}" \
  mysql:8 \
  --lower_case_table_names=1 >/dev/null

docker run -d \
  --name "${VERIEQL_CONTAINER_NAME}" \
  --network "${VERIEQL_NETWORK}" \
  --restart unless-stopped \
  "${VERIEQL_SIDECAR_IMAGE}" >/dev/null

echo "Started ${VERIEQL_CONTAINER_NAME} and ${VERIEQL_MYSQL_CONTAINER_NAME} on network ${VERIEQL_NETWORK}."
