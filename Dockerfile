FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    UVICORN_WORKERS=2

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY web_app/requirements-prod.txt /tmp/requirements-prod.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements-prod.txt

COPY . .

RUN adduser --disabled-password --gecos "" appuser && \
    mkdir -p /app/web_app/jobs && \
    chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

CMD ["sh", "-c", "uvicorn web_app.backend.main:app --host 0.0.0.0 --port ${PORT} --workers ${UVICORN_WORKERS}"]

