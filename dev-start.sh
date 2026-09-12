#!/usr/bin/env bash
# Personal Agent development startup.
#
# Default: lightweight native FastAPI + native Ollama.
# Full:    Colima/Docker Compose observability stack.

set -euo pipefail

cd "$(dirname "$0")"

REQUIRED_MODELS=(qwen2.5:7b nomic-embed-text)
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
FASTAPI_URL="${FASTAPI_URL:-http://127.0.0.1:8000}"
RUNTIME_DIR=".runtime"
VENV_DIR=".venv"
FASTAPI_PID="$RUNTIME_DIR/fastapi.pid"
OLLAMA_PID="$RUNTIME_DIR/ollama.pid"
FASTAPI_LOG="$RUNTIME_DIR/fastapi.log"
OLLAMA_LOG="$RUNTIME_DIR/ollama.log"
REQ_STAMP="$RUNTIME_DIR/requirements.sha256"
FASTAPI_LANGFUSE_ENABLED="false"

FULL=0
REBUILD=0

usage() {
  cat <<'EOF'
Usage:
  ./dev-start.sh
  ./dev-start.sh --full
  ./dev-start.sh --full --rebuild
EOF
}

for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --rebuild) REBUILD=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

mkdir -p "$RUNTIME_DIR"

health_ok() {
  curl -sf "$1" >/dev/null 2>&1
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local tries="${3:-60}"
  echo -n "  Waiting for $label"
  for _ in $(seq 1 "$tries"); do
    if health_ok "$url"; then
      echo
      return 0
    fi
    echo -n "."
    sleep 1
  done
  echo
  return 1
}

ensure_env_files() {
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "  Created .env from .env.example"
  fi
}

load_root_env() {
  if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
  fi
}

ensure_ollama() {
  if ! command -v ollama >/dev/null 2>&1; then
    echo "✗ ollama not found — install it first (for example: brew install ollama)" >&2
    exit 1
  fi

  if health_ok "$OLLAMA_URL/api/version"; then
    rm -f "$OLLAMA_PID"
    echo "✓ Ollama already running at $OLLAMA_URL"
    return
  fi

  echo "  Starting Ollama (log: $OLLAMA_LOG)..."
  nohup ollama serve </dev/null >"$OLLAMA_LOG" 2>&1 &
  echo "$!" >"$OLLAMA_PID"

  if ! wait_for_url "$OLLAMA_URL/api/version" "Ollama" 30; then
    echo "✗ Ollama failed to start — see $OLLAMA_LOG" >&2
    exit 1
  fi
  echo "✓ Ollama running at $OLLAMA_URL"
}

ensure_models() {
  local present
  present="$(ollama list | tail -n +2 | awk '{print $1}')"
  for model in "${REQUIRED_MODELS[@]}"; do
    local base="${model%%:*}"
    if ! grep -q "^${base}" <<<"$present"; then
      echo "  Pulling $model (first time only; this can take several minutes)..."
      ollama pull "$model"
      present="$(ollama list | tail -n +2 | awk '{print $1}')"
    fi
    echo "✓ $model available"
  done
}

ensure_python_env() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ python3 not found — install Python first" >&2
    exit 1
  fi

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "  Creating Python virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
  fi

  local current_hash
  current_hash="$(shasum -a 256 server/requirements.txt | awk '{print $1}')"
  local installed_hash=""
  if [ -f "$REQ_STAMP" ]; then
    installed_hash="$(cat "$REQ_STAMP")"
  fi
  if [ "$current_hash" != "$installed_hash" ]; then
    echo "  Installing Python dependencies..."
    "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
    "$VENV_DIR/bin/python" -m pip install -r server/requirements.txt
    echo "$current_hash" >"$REQ_STAMP"
  fi
  echo "✓ Python environment ready"
}

start_fastapi_native() {
  if health_ok "$FASTAPI_URL/health"; then
    echo "✓ FastAPI already running at $FASTAPI_URL"
    return
  fi

  if [ -f "$FASTAPI_PID" ]; then
    local pid
    pid="$(cat "$FASTAPI_PID" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
      echo "  FastAPI process $pid exists; waiting for health..."
      if wait_for_url "$FASTAPI_URL/health" "FastAPI" 20; then
        echo "✓ FastAPI running at $FASTAPI_URL"
        return
      fi
      echo "✗ FastAPI process exists but is not healthy — see $FASTAPI_LOG" >&2
      exit 1
    fi
  fi

  echo "  Starting FastAPI natively (log: $FASTAPI_LOG)..."
  LANGFUSE_ENABLED="$FASTAPI_LANGFUSE_ENABLED" \
  OLLAMA_BASE_URL="$OLLAMA_URL" \
  OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-30m}" \
  nohup "$VENV_DIR/bin/python" -m uvicorn server.main:app \
    --host 127.0.0.1 --port 8000 \
    --reload --reload-dir server \
    </dev/null \
    >"$FASTAPI_LOG" 2>&1 &
  echo "$!" >"$FASTAPI_PID"

  if ! wait_for_url "$FASTAPI_URL/health" "FastAPI" 60; then
    echo "✗ FastAPI failed to become healthy — see $FASTAPI_LOG" >&2
    exit 1
  fi
  echo "✓ FastAPI running at $FASTAPI_URL"
}

stop_project_fastapi_if_owned() {
  if [ ! -f "$FASTAPI_PID" ]; then
    return
  fi
  local pid
  pid="$(cat "$FASTAPI_PID" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
    echo "  Restarting project-owned FastAPI for full-mode settings..."
    kill "$pid" >/dev/null 2>&1 || true
    for _ in $(seq 1 10); do
      if ! kill -0 "$pid" >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
  fi
  rm -f "$FASTAPI_PID"
}

stop_compose_fastapi_if_available() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker compose stop personal-agent >/dev/null 2>&1 || true
  fi
}

ensure_container_runtime() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "✗ Full mode requires the Docker CLI." >&2
    echo "  Install with: brew install colima docker docker-compose" >&2
    exit 1
  fi

  if docker info >/dev/null 2>&1; then
    echo "✓ Docker daemon available"
    return
  fi

  if command -v colima >/dev/null 2>&1; then
    echo "  Starting Colima..."
    colima start
    if wait_for_docker; then
      echo "✓ Docker daemon available through Colima"
      return
    fi
    echo "✗ Colima started, but Docker daemon is not reachable" >&2
    exit 1
  fi

  echo "✗ Full mode requires Colima when no Docker daemon is running." >&2
  echo "  Install with: brew install colima docker docker-compose" >&2
  exit 1
}

wait_for_docker() {
  for _ in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_full_stack() {
  ensure_env_files
  load_root_env
  ensure_ollama
  ensure_models
  ensure_python_env
  ensure_container_runtime

  if [ "$REBUILD" -eq 1 ]; then
    echo "  Starting Docker Compose observability stack with rebuild..."
    docker compose build personal-agent
    docker compose up -d --build postgres clickhouse redis minio langfuse-web langfuse-worker
  else
    echo "  Starting Docker Compose observability stack..."
    docker compose up -d postgres clickhouse redis minio langfuse-web langfuse-worker
  fi

  if ! wait_for_url "http://127.0.0.1:3000/api/public/health" "Langfuse" 90; then
    echo "✗ Langfuse did not become healthy. Check: docker compose logs -f langfuse-web" >&2
    exit 1
  fi
  stop_project_fastapi_if_owned
  stop_compose_fastapi_if_available
  FASTAPI_LANGFUSE_ENABLED="true"
  export LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-http://localhost:3000}"
  export LANGFUSE_PUBLIC_KEY="${LANGFUSE_INIT_PROJECT_PUBLIC_KEY:-${LANGFUSE_PUBLIC_KEY:-}}"
  export LANGFUSE_SECRET_KEY="${LANGFUSE_INIT_PROJECT_SECRET_KEY:-${LANGFUSE_SECRET_KEY:-}}"
  start_fastapi_native

  echo
  echo "Personal Agent full development ready."
  echo
  echo "  FastAPI:  $FASTAPI_URL"
  echo "  Ollama:   $OLLAMA_URL"
  echo "  Langfuse: http://localhost:3000"
  echo "  MinIO:    http://localhost:9001   (login: minio / miniosecret)"
}

start_lightweight() {
  ensure_env_files
  ensure_ollama
  ensure_models
  ensure_python_env
  stop_compose_fastapi_if_available
  start_fastapi_native

  echo
  echo "Personal Agent ready."
  echo
  echo "  FastAPI: $FASTAPI_URL"
  echo "  Ollama:  $OLLAMA_URL"
  echo
  echo "  Chrome extension: reload from chrome://extensions if needed."
  echo "  Logs:  tail -f $FASTAPI_LOG"
  echo "  Stop:  ./dev-stop.sh"
}

if [ "$FULL" -eq 1 ]; then
  echo "Personal Agent — full development"
  echo
  start_full_stack
else
  echo "Personal Agent — lightweight development"
  echo
  start_lightweight
fi
