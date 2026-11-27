#!/usr/bin/env sh
# Regenerate backend/requirements.lock: exact versions with SHA-256 hashes for linux/amd64, CPython 3.11.
#
# pip-compile runs inside the same base image as deploy/Dockerfile, so the resolution matches the
# production image whatever machine runs this script. Requires Docker. On Windows use
# scripts/lock-requirements.ps1 instead.
#
#   scripts/lock-requirements.sh             keep existing pins that requirements.in still allows
#   scripts/lock-requirements.sh --upgrade   move every package to the newest allowed version
#
# Environment:
#   PYTHON_IMAGE    base image (default python:3.11-slim); use the same tag or digest as the Dockerfile
#   PIP_TOOLS_SPEC  pip-tools requirement installed in the container (default pip-tools>=7.4)
#
# Review the diff of backend/requirements.lock and run the test image before committing it.
set -eu

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
image=${PYTHON_IMAGE:-python:3.11-slim}
pip_tools=${PIP_TOOLS_SPEC:-pip-tools>=7.4}
upgrade=""

for arg in "$@"; do
  case "$arg" in
    --upgrade) upgrade="--upgrade" ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi

# $PIP_TOOLS_SPEC and $UPGRADE are expanded by the container shell (single quotes are intentional).
docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_NO_CACHE_DIR=1 \
  -e PIP_TOOLS_SPEC="$pip_tools" -e UPGRADE="$upgrade" \
  -v "$repo_root/backend:/work" -w /work \
  "$image" \
  sh -c 'python -m venv /tmp/tools && /tmp/tools/bin/pip install --quiet $PIP_TOOLS_SPEC && /tmp/tools/bin/pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras $UPGRADE --output-file requirements.lock requirements.in'

echo "wrote $repo_root/backend/requirements.lock"
