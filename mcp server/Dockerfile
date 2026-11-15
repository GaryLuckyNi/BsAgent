FROM python:3.11-slim-bookworm

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY ["mcp server/pyproject.toml", "/app/pyproject.toml"]
COPY ["mcp server/app", "/app/app"]

RUN pip install --upgrade pip==26.1.2 setuptools==83.0.0 wheel==0.47.0 \
    && pip install . \
    && groupadd --gid 10001 mcp \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin mcp \
    && chown -R mcp:mcp /app

USER mcp
EXPOSE 8011

CMD ["python", "-m", "app.main"]
