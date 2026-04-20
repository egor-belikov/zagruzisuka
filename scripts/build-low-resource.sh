#!/usr/bin/env bash
# Сборка без параллельной распаковки слоёв и без высокого приоритета I/O (слабый VPS).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export DOCKER_BUILDKIT=1
export BUILDKIT_MAX_PARALLELISM="${BUILDKIT_MAX_PARALLELISM:-1}"
export COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-1}"
# В Docker Compose v5 у `build` нет --parallel; параллелизм BuildKit режем через BUILDKIT_MAX_PARALLELISM.
exec nice -n 15 ionice -c2 -n7 docker compose \
  -f docker-compose.yml \
  -f docker-compose.egorvps.yml \
  build "$@"
