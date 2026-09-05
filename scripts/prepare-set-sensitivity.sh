#!/usr/bin/env bash
# Reuse semantics-invariant artifacts for the set-sensitivity execution run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${1:-${REPO_ROOT}/outputs/bird-full-rerun-bag}"
TARGET_DIR="${2:-${REPO_ROOT}/outputs/bird-full-rerun-set}"

if [[ ! -d "${SOURCE_DIR}" ]]; then
  echo "Source artifact directory not found: ${SOURCE_DIR}" >&2
  exit 1
fi

if [[ "${SOURCE_DIR}" == "${TARGET_DIR}" ]]; then
  echo "Source and target artifact directories must differ." >&2
  exit 1
fi

required_artifacts=(
  normalized_examples.jsonl
  mutants.jsonl
  equivalence.jsonl
)

for name in "${required_artifacts[@]}"; do
  if [[ ! -f "${SOURCE_DIR}/${name}" ]]; then
    echo "Required source artifact not found: ${SOURCE_DIR}/${name}" >&2
    exit 1
  fi
done

mkdir -p "${TARGET_DIR}"
for name in "${required_artifacts[@]}"; do
  if [[ -e "${TARGET_DIR}/${name}" ]] && \
    ! cmp -s "${SOURCE_DIR}/${name}" "${TARGET_DIR}/${name}"; then
    echo "Refusing to replace different target: ${TARGET_DIR}/${name}" >&2
    exit 1
  fi
done

for name in "${required_artifacts[@]}"; do
  if [[ ! -e "${TARGET_DIR}/${name}" ]]; then
    cp -p "${SOURCE_DIR}/${name}" "${TARGET_DIR}/${name}"
  fi
done

echo "Prepared set-sensitivity inputs in ${TARGET_DIR}."
