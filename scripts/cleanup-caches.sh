#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--dry-run]" >&2
  exit 2
fi

remove_path() {
  local path="$1"
  if [[ "$DRY_RUN" == true ]]; then
    printf 'would remove %s\n' "$path"
  else
    rm -rf -- "$path"
    printf 'removed %s\n' "$path"
  fi
}

while IFS= read -r -d '' path; do
  remove_path "$path"
done < <(
  find "$ROOT_DIR" \
    \( -path "$ROOT_DIR/.git" -o -path "$ROOT_DIR/.venv" \) -prune -o \
    -type d \( \
      -path "$ROOT_DIR/tmp-assets" -o \
      -path "$ROOT_DIR/frontend/node_modules" -o \
      -path "$ROOT_DIR/frontend/dist" -o \
      -path "$ROOT_DIR/frontend/build" -o \
      -path "$ROOT_DIR/frontend/.npm" -o \
      -path "$ROOT_DIR/frontend/.cache" -o \
      -name __pycache__ -o \
      -name .pytest_cache -o \
      -name .ruff_cache -o \
      -name .mypy_cache -o \
      -name .vite -o \
      -name coverage \
    \) -prune -print0
)

while IFS= read -r -d '' path; do
  remove_path "$path"
done < <(
  find "$ROOT_DIR" \
    \( \
      -path "$ROOT_DIR/.git" -o \
      -path "$ROOT_DIR/.venv" -o \
      -type d \( \
        -path "$ROOT_DIR/tmp-assets" -o \
        -path "$ROOT_DIR/frontend/node_modules" -o \
        -path "$ROOT_DIR/frontend/dist" -o \
        -path "$ROOT_DIR/frontend/build" -o \
        -path "$ROOT_DIR/frontend/.npm" -o \
        -path "$ROOT_DIR/frontend/.cache" -o \
        -name __pycache__ -o \
        -name .pytest_cache -o \
        -name .ruff_cache -o \
        -name .mypy_cache -o \
        -name .vite -o \
        -name coverage \
      \) \
    \) -prune -o \
    -type f \( \
      -name '*.pyc' -o \
      -name '*.pyo' -o \
      -name '*.tsbuildinfo' -o \
      -name 'npm-debug.log*' -o \
      -name 'yarn-debug.log*' -o \
      -name 'yarn-error.log*' -o \
      -name 'pnpm-debug.log*' -o \
      -name '.coverage' -o \
      -name '.DS_Store' \
    \) -print0
)

echo "Cache cleanup complete."
