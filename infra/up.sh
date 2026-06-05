#!/usr/bin/env bash
# Bring up the real backends (Postgres + ClickHouse) for local app dev.
# Then run with:  eval-engine run examples/capitals_qa.yaml
# (The test suite self-provisions these — see tests/conftest.py — so this is only for running the app.)
set -euo pipefail

docker rm -f ee-postgres ee-clickhouse >/dev/null 2>&1 || true

docker run -d --name ee-postgres \
  -e POSTGRES_PASSWORD=evalengine -e POSTGRES_USER=evalengine -e POSTGRES_DB=evalengine \
  -p 5433:5432 postgres:16

docker run -d --name ee-clickhouse --ulimit nofile=262144:262144 \
  -p 8123:8123 -p 9000:9000 clickhouse/clickhouse-server:24

echo "waiting for readiness..."
until docker exec ee-postgres pg_isready -U evalengine >/dev/null 2>&1; do sleep 1; done
until curl -sf http://localhost:8123/ping >/dev/null 2>&1; do sleep 1; done
echo "postgres :5433 and clickhouse :8123 ready ✓"
