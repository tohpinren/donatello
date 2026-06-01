#!/usr/bin/env bash
# Run the Donatello pipeline inside the resource-limited runner container.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  run-pipeline.sh --config <config> --bird-root <host_bird_root> [options] [-- <extra donatello args>]

Options:
  --config <path>         Config path inside the repo. Default: configs/experiments/bird-full-pipeline.yaml
  --bird-root <path>      Host BIRD root to mount read-only into /opt/donatello/data/bird
  --outputs-root <path>   Host outputs root to mount read-write into /opt/donatello/outputs
  --cpus <value>          Docker CPU limit. Default: 16
  --memory <value>        Docker memory limit. Default: 200g
  --image <name>          Runner image name. Default: donatello-runner:latest
  --name <name>           Container name. Default: donatello-pipeline
  --force                 Replace an existing pipeline container with the same name
  --help                  Show this message

Example:
  scripts/run-pipeline.sh \
    --config configs/experiments/bird-full-pipeline.yaml \
    --bird-root /mnt/data/bird \
    --outputs-root /path/to/donatello/outputs \
    --cpus 16 \
    --memory 200g
EOF
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CONFIG_PATH="configs/experiments/bird-full-pipeline.yaml"
BIRD_ROOT_HOST=""
OUTPUTS_ROOT_HOST="${REPO_ROOT}/outputs"
DONATELLO_RUNNER_IMAGE="${DONATELLO_RUNNER_IMAGE:-donatello-runner:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-donatello-pipeline}"
CPU_LIMIT="${CPU_LIMIT:-16}"
MEMORY_LIMIT="${MEMORY_LIMIT:-200g}"
CONTAINER_UID="${CONTAINER_UID:-$(id -u)}"
CONTAINER_GID="${CONTAINER_GID:-$(id -g)}"
DOCKER_SOCK_GID="${DOCKER_SOCK_GID:-}"
FORCE_REPLACE="${FORCE_REPLACE:-0}"

EXTRA_DONATELLO_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_PATH="$2"
      shift 2
      ;;
    --bird-root)
      BIRD_ROOT_HOST="$2"
      shift 2
      ;;
    --outputs-root)
      OUTPUTS_ROOT_HOST="$2"
      shift 2
      ;;
    --cpus)
      CPU_LIMIT="$2"
      shift 2
      ;;
    --memory)
      MEMORY_LIMIT="$2"
      shift 2
      ;;
    --image)
      DONATELLO_RUNNER_IMAGE="$2"
      shift 2
      ;;
    --name)
      CONTAINER_NAME="$2"
      shift 2
      ;;
    --force)
      FORCE_REPLACE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      EXTRA_DONATELLO_ARGS=("$@")
      break
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "${BIRD_ROOT_HOST}" ]]; then
  echo "--bird-root is required." >&2
  usage >&2
  exit 1
fi

if [[ ! -f "${REPO_ROOT}/${CONFIG_PATH}" ]]; then
  echo "Config not found in repo: ${CONFIG_PATH}" >&2
  exit 1
fi

if [[ ! -d "${BIRD_ROOT_HOST}" ]]; then
  echo "BIRD root not found: ${BIRD_ROOT_HOST}" >&2
  exit 1
fi

mkdir -p "${OUTPUTS_ROOT_HOST}"

if [[ -z "${DOCKER_SOCK_GID}" && -S /var/run/docker.sock ]]; then
  DOCKER_SOCK_GID="$(stat -c '%g' /var/run/docker.sock)"
fi

docker_group_args=()
if [[ -n "${DOCKER_SOCK_GID}" ]]; then
  docker_group_args+=(--group-add "${DOCKER_SOCK_GID}")
fi

if docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
  if [[ "${FORCE_REPLACE}" != "1" ]]; then
    echo "Refusing to replace existing container: ${CONTAINER_NAME}" >&2
    echo "Re-run with --force to replace it explicitly." >&2
    exit 1
  fi

  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
fi

docker run --rm \
  --name "${CONTAINER_NAME}" \
  --init \
  --user "${CONTAINER_UID}:${CONTAINER_GID}" \
  "${docker_group_args[@]}" \
  --cpus "${CPU_LIMIT}" \
  --memory "${MEMORY_LIMIT}" \
  -e HOME=/tmp/donatello-home \
  -e XDG_CACHE_HOME=/tmp/donatello-xdg-cache \
  -e UV_CACHE_DIR=/tmp/donatello-uv-cache \
  -v "${BIRD_ROOT_HOST}:/opt/donatello/data/bird:ro" \
  -v "${OUTPUTS_ROOT_HOST}:/opt/donatello/outputs:rw" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w /opt/donatello \
  "${DONATELLO_RUNNER_IMAGE}" \
  uv run --no-sync donatello run --config "${CONFIG_PATH}" "${EXTRA_DONATELLO_ARGS[@]}"
