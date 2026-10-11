# syntax=docker/dockerfile:1

# ---- runtime image ---------------------------------------------------------
# Pinned by digest for reproducible builds (update deliberately to pick up security fixes).
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Unprivileged user; /data/images is the only writable location.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data/images \
 && chown 10001:10001 /data/images
USER 10001

ENV DATA_DIR=/data/images
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--no-access-log", "--limit-concurrency", "64", "--timeout-keep-alive", "5"]

# ---- test image: `docker build --target test` -------------------------------
FROM runtime AS test
USER root
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY tests ./tests
USER 10001
ENV TOOL_API_KEY=test-api-key-0123456789abcdef
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
