# FastAPI backend for Personal Agent. Ollama stays native on the host
# (Metal acceleration). This image only ships the Python server.

FROM python:3.11-slim

WORKDIR /app

# httpx.get() uses OpenSSL from the base image; nothing else system-level
# is needed for the current requirements.txt.
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

# Copy source. In `compose.yaml` a bind mount overlays /app/server for
# hot-reload during development, so this COPY primarily supports
# non-mounted (production-style) runs.
COPY server /app/server

EXPOSE 8000

# --reload watches /app/server for changes; combined with the compose
# bind mount this gives edit-and-refresh without rebuilding the image.
CMD ["uvicorn", "server.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--reload", "--reload-dir", "/app/server"]
