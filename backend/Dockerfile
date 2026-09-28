FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first (hash-pinned) for layer caching.
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY pyproject.toml alembic.ini ./
COPY src ./src
RUN pip install --no-deps . \
    && useradd --system --uid 10001 --no-create-home app

USER app
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

CMD ["uvicorn", "incident_intel.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
