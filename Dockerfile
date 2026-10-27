# syntax=docker/dockerfile:1

# ---- Stage 1: fetch the Vosk Persian model (kept out of the final layer history) ----
FROM python:3.12-slim AS vosk-model
ARG VOSK_MODEL=vosk-model-small-fa-0.42
ARG VOSK_MODEL_URL=https://alphacephei.com/vosk/models/${VOSK_MODEL}.zip
# Optional: pin the archive hash for a reproducible, tamper-evident build.
ARG VOSK_MODEL_SHA256=
RUN python - <<'PY'
import hashlib, os, shutil, sys, urllib.request, zipfile
url, want = os.environ["VOSK_MODEL_URL"], os.environ.get("VOSK_MODEL_SHA256", "").strip().lower()
name = os.environ["VOSK_MODEL"]
print(f"Downloading {url}", flush=True)
urllib.request.urlretrieve(url, "/tmp/model.zip")
got = hashlib.sha256(open("/tmp/model.zip", "rb").read()).hexdigest()
print(f"sha256 {got}", flush=True)
if want and got != want:
    sys.exit(f"Vosk model hash mismatch: expected {want}, got {got}")
with zipfile.ZipFile("/tmp/model.zip") as zf:
    zf.extractall("/tmp/extract")
shutil.move(f"/tmp/extract/{name}", "/opt/vosk-model")
PY

# ---- Stage 2: application ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg converts Telegram OGG/Opus to 16 kHz mono WAV for Vosk.
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

# Model before code: changing code does not re-copy 50 MB.
COPY --from=vosk-model /opt/vosk-model /opt/vosk-model

COPY --chown=app:app . .

# Created in the image so a fresh named volume inherits the app user's ownership.
RUN mkdir -p /data/voice && chown -R app:app /data

USER app

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
