#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.yml"
APP_SERVICES=(backend frontend nginx port-router)
BUILD_ARGS=()
UP_ARGS=(-d --remove-orphans)

usage() {
  cat <<'EOF'
Usage: scripts/docker-build-run.sh [options]

Build the application images, start the complete Docker Compose stack, and
wait for the backend health endpoint.

Options:
  --no-cache        Build application images without Docker layer cache.
  --pull            Pull newer base images before building.
  --force-recreate  Recreate all Compose services after building.
  --skip-build      Start the stack without building images.
  -h, --help        Show this help.
EOF
}

SKIP_BUILD=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-cache)
      BUILD_ARGS+=(--no-cache)
      ;;
    --pull)
      BUILD_ARGS+=(--pull)
      ;;
    --force-recreate)
      UP_ARGS+=(--force-recreate)
      ;;
    --skip-build)
      SKIP_BUILD=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed or not available in PATH." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose is not available." >&2
  exit 1
fi

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

if [[ "$SKIP_BUILD" == false ]]; then
  echo "Building: ${APP_SERVICES[*]}"
  compose build "${BUILD_ARGS[@]}" "${APP_SERVICES[@]}"
fi

echo "Starting Docker Compose stack..."
compose up "${UP_ARGS[@]}"

echo "Waiting for backend health..."
for attempt in $(seq 1 30); do
  if compose exec -T backend python -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=2).read()' \
    >/dev/null 2>&1; then
    echo "Backend is healthy."
    compose exec -T backend python -c \
      'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=2).read().decode())'
    compose ps
    exit 0
  fi
  sleep 2
done

echo "Backend did not become healthy within 60 seconds." >&2
compose ps >&2
compose logs --tail=120 backend >&2
exit 1
