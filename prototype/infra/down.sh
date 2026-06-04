#!/usr/bin/env bash
# Tear down the prototype backends.
docker rm -f ee-postgres ee-clickhouse >/dev/null 2>&1 || true
echo "removed ee-postgres, ee-clickhouse"
