#!/usr/bin/env bash
# Stop Personal Agent development processes.
#
# Default: stop only native processes started by ./dev-start.sh.
# Full:    also stop the Docker Compose observability stack.

set -euo pipefail
cd "$(dirname "$0")"

RUNTIME_DIR=".runtime"
FASTAPI_PID="$RUNTIME_DIR/fastapi.pid"
OLLAMA_PID="$RUNTIME_DIR/ollama.pid"

FULL=0

usage() {
  cat <<'EOF'
Usage:
  ./dev-stop.sh
  ./dev-stop.sh --full
EOF
}

for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

stop_pid_file() {
  local pid_file="$1"
  local label="$2"
  if [ ! -f "$pid_file" ]; then
    echo "  $label was not started by this project."
    return
  fi
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  rm -f "$pid_file"
  if [ -z "$pid" ] || ! kill -0 "$pid" >/dev/null 2>&1; then
    echo "  $label is not running."
    return
  fi
  echo "  Stopping $label (pid $pid)..."
  kill "$pid" >/dev/null 2>&1 || true
  for _ in $(seq 1 10); do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      echo "✓ $label stopped"
      return
    fi
    sleep 1
  done
  echo "  $label did not exit after SIGTERM; sending SIGKILL..."
  kill -9 "$pid" >/dev/null 2>&1 || true
  echo "✓ $label stopped"
}

echo "Stopping Personal Agent lightweight processes"
stop_pid_file "$FASTAPI_PID" "FastAPI"
stop_pid_file "$OLLAMA_PID" "Ollama"

if [ "$FULL" -eq 1 ]; then
  echo
  echo "Stopping Docker Compose services"
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker compose down
    echo "✓ Compose stack stopped. Persistent volumes kept."
    echo "  To wipe Postgres/ClickHouse/MinIO data: docker compose down -v"
  else
    echo "! Docker daemon is not reachable; Compose stack was not stopped."
  fi
fi
