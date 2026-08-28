#!/usr/bin/env bash
# Stop the Personal Agent stack. Preserves data volumes.
#   - Postgres, ClickHouse, and MinIO data survive.
#   - server/memory.db and server/profile.json live on the host, untouched.
#   - Ollama keeps running natively; stop it manually if you want:
#         pkill -x ollama

set -euo pipefail
cd "$(dirname "$0")"

docker compose down
echo "Stack stopped. Persistent volumes kept."
echo "To also wipe Postgres/ClickHouse/MinIO data: docker compose down -v"
