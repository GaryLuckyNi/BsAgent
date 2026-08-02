FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
COPY ocr-service/pyproject.toml ./pyproject.toml
COPY ocr-service/requirements.lock ./requirements.lock
COPY ocr-service/ocr_service ./ocr_service
RUN pip install --upgrade pip==26.1.2 setuptools==83.0.0 wheel==0.47.0 \
    && pip install --requirement requirements.lock \
    && pip install --no-deps --no-build-isolation . \
    && pip check


FROM python:3.11-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PADDLE_PDX_CACHE_HOME=/models

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10002 ocr \
    && useradd --uid 10002 --gid 10002 --create-home --shell /usr/sbin/nologin ocr \
    && mkdir -p /models \
    && chown -R ocr:ocr /models

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
USER ocr
EXPOSE 8000

CMD ["uvicorn", "ocr_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
