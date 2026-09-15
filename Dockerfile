# syntax=docker/dockerfile:1

# Alpine works here because every runtime dependency is pure Python or ships a
# musllinux wheel (see requirements.txt). No compiler is installed and none is
# needed — if a dependency bump ever breaks that, switch this line to
# python:3.12-slim rather than adding gcc/musl-dev, which would put a build
# toolchain in the runtime image.
FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/zer0space-net/zer0space-music" \
      org.opencontainers.image.description="zer0space Music — homelab music player and scraper"

# PYTHONDONTWRITEBYTECODE: the container filesystem is throwaway, so .pyc files
# are pure noise. PYTHONUNBUFFERED: without it print() output sits in a buffer
# and `docker service logs` shows nothing until the process exits — which is
# exactly when you need the logs most.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies before source: this layer stays cached across every commit that
# does not touch requirements.txt, which is nearly all of them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY static/ ./static/
COPY templates/ ./templates/

# The app never writes to its own filesystem: nothing is downloaded to disk
# (yt-dlp runs with skip_download and cachedir disabled) and audio is streamed
# through, never stored. So it runs unprivileged.
RUN adduser -D -H -u 10001 zer0space
USER 10001

EXPOSE 8000

# Liveness only, deliberately not touching PostgreSQL: a health check that
# failed during a database outage would have Swarm restart a container that is
# behaving exactly as designed — and this service keeps browsing and playing
# while the database is away.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# More than one worker is fine here, unlike the dashboard: this service holds no
# in-process session state. Every per-user fact lives in PostgreSQL and identity
# arrives in a header on each request. Two workers keep a slow yt-dlp extraction
# from blocking unrelated requests — the extraction itself already runs in a
# thread, but the process still has one event loop.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
