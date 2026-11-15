FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY pyproject.toml ./
COPY backend/requirements.lock ./requirements.lock
COPY backend ./backend
RUN pip install --upgrade pip==26.1.2 setuptools==83.0.0 wheel==0.47.0 \
    && pip install --requirement requirements.lock \
    && pip install --no-deps --no-build-isolation . \
    && pip check
FROM python:3.11-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bsagent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin bsagent

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY backend ./backend
COPY deploy/run-migrations.sh ./deploy/run-migrations.sh

RUN chmod 0555 /app/deploy/run-migrations.sh \
    && chown -R bsagent:bsagent /app

USER bsagent
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
