#!/usr/bin/env bash
# Run tests/integration against a disposable database; the real database is never touched.
# Extraction/embedding use the providers configured in .env, so this makes real model calls.
set -euo pipefail
cd "$(dirname "$0")/.."

DB_NAME="${INTEGRATION_DB_NAME:-policy_integration_test}"
case "$DB_NAME" in *_test) ;; *) echo "INTEGRATION_DB_NAME must end with _test" >&2; exit 2;; esac
TEST_URL="postgresql+psycopg://policy:policy@postgres:5432/${DB_NAME}"

cleanup() {
  docker compose exec -T postgres dropdb -U policy --if-exists "$DB_NAME" >/dev/null
}
trap cleanup EXIT

cleanup
docker compose exec -T postgres createdb -U policy "$DB_NAME"
docker compose run --rm -T -e DATABASE_URL="$TEST_URL" migrate >/dev/null
docker compose run --rm -T \
  -e DATABASE_URL="$TEST_URL" -e AUTO_PUBLISH=false \
  -e PYTHONPATH=/workspace/src:/workspace -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/workspace" -w /workspace worker \
  python -m unittest discover -s tests/integration -v "$@"
