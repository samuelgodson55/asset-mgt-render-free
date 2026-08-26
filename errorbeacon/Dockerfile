# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY errorbeacon/requirements.txt .
# --mount=type=cache persists pip's downloaded wheels in a BuildKit cache
# volume OUTSIDE this layer, same pattern as backend/Dockerfile's builder
# stage -- so a rerun of this RUN (fresh requirements.txt, --no-cache
# build, pruned layer cache, etc.) reuses whatever was already downloaded
# instead of re-fetching every package from PyPI from zero. Matches
# backend/Dockerfile's --default-timeout=100 too, since this service
# previously had no cushion at all on a slow/high-latency connection.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --default-timeout=100 --retries=5 -r requirements.txt
COPY errorbeacon/app ./app
COPY shared ./shared
RUN useradd --create-home --uid 10001 appuser && mkdir -p /data && chown -R appuser:appuser /app /data
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
# ErrorBeacon uses internal threads for alert/AI concurrency. Keep one Uvicorn
# process because its SQLite store is local and should have one process owner.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
