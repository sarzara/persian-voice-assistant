# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg converts Telegram OGG/Opus to 16 kHz mono WAV for Vosk (batch 02).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user with a fixed UID, so volume ownership is predictable.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .

# Created in the image so a fresh named volume inherits the app user's ownership.
RUN mkdir -p /data/voice && chown -R app:app /data

USER app

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
