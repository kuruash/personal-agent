#!/usr/bin/env bash
# One-command development startup.
#   1. verify Docker + Ollama
#   2. start Ollama if it isn't already
#   3. pull Qwen + nomic-embed-text if missing
#   4. copy .env.example → .env on first run
#   5. bring up the Docker Compose stack

set -euo pipefail

cd "$(dirname "$0")"

REQUIRED_MODELS=(qwen2.5:7b nomic-embed-text)
OLLAMA_URL="http://127.0.0.1:11434"

echo "Personal Agent development stack"
echo

# ── Docker ────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  echo "✗ docker not found — install Docker Desktop first" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "✗ Docker daemon not reachable — start Docker Desktop first" >&2
  exit 1
fi
echo "✓ Docker running"

# ── Ollama (native on host) ───────────────────────────────────────────
if ! command -v ollama >/dev/null 2>&1; then
  echo "✗ ollama not found — install it (e.g. brew install ollama) and rerun" >&2
  exit 1
fi
if ! curl -sf "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  echo "  Starting 'ollama serve' in the background (log: /tmp/ollama.log)..."
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  for _ in $(seq 1 20); do
    sleep 1
    if curl -sf "$OLLAMA_URL/api/version" >/dev/null 2>&1; then break; fi
  done
  if ! curl -sf "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
    echo "✗ Ollama failed to come up — see /tmp/ollama.log" >&2
    exit 1
  fi
fi
echo "✓ Ollama running at $OLLAMA_URL"

# ── Models ────────────────────────────────────────────────────────────
present="$(ollama list | tail -n +2 | awk '{print $1}')"
for model in "${REQUIRED_MODELS[@]}"; do
  # ollama list uses "name:tag" or "name:latest"; strip the tag for compare.
  base="${model%%:*}"
  if ! grep -q "^${base}" <<<"$present"; then
    echo "  Pulling $model (first time only — this can take several minutes)..."
    ollama pull "$model"
  fi
done
echo "✓ Ollama models present: ${REQUIRED_MODELS[*]}"

# ── .env ──────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  Created .env from .env.example — edit it if you want non-default secrets"
fi

# ── Compose ───────────────────────────────────────────────────────────
echo "  Building and starting containers..."
docker compose up -d --build

# Wait for readiness so URLs are actually live when we print them.
echo -n "  Waiting for services to become healthy"
ready=0
for _ in $(seq 1 90); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 \
     && curl -sf http://127.0.0.1:3000/api/public/health >/dev/null 2>&1; then
    ready=1; break
  fi
  echo -n "."; sleep 2
done
echo

if [ "$ready" -eq 1 ]; then
  echo "✓ FastAPI healthy"
  echo "✓ Langfuse healthy"
else
  echo "! Services didn't report healthy within the timeout. Check:"
  echo "    docker compose ps"
  echo "    docker compose logs -f personal-agent langfuse-web"
fi

echo
echo "  FastAPI:  http://localhost:8000"
echo "  Langfuse: http://localhost:3000"
echo "  MinIO:    http://localhost:9001   (login: minio / miniosecret)"
echo
echo "  Chrome extension: reload from chrome://extensions if needed."
echo "  Logs:  docker compose logs -f            (or  ... -f personal-agent)"
echo "  Stop:  ./dev-stop.sh                     (or  docker compose down)"
