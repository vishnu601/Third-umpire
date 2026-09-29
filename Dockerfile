FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_DB_PATH=/data/db.sqlite3 \
    DJANGO_MEDIA_ROOT=/data/media \
    DOGFOOD_FIXTURES=/app/fixtures.json

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ /app/src/
COPY fixtures.json /app/fixtures.json
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && useradd --system --uid 10001 --home-dir /app portal \
    && mkdir -p /data && chown portal /data
USER portal

WORKDIR /app/src
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
ENTRYPOINT ["/app/entrypoint.sh"]
