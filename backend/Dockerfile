# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Generous timeouts/retries: a slow connection to PyPI should not fail the build.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10

WORKDIR /app

# Dependencies first (hash-pinned) for layer caching.
# The cache mount keeps downloaded packages between builds without adding them to the
# image, so a changed lock file only downloads what is new.
COPY requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r requirements.lock

COPY pyproject.toml alembic.ini ./
COPY src ./src
RUN pip install --no-deps . \
    && useradd --system --uid 10001 --no-create-home app

USER app
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

CMD ["uvicorn", "incident_intel.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
