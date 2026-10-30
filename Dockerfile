# syntax=docker/dockerfile:1
# Base images and package indexes are build arguments so the image can be built where
# Docker Hub / PyPI / Debian need a mirror (see docs/DEPLOY.md "Outbound access").
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---- Stage 1: fetch the Vosk Persian model (kept out of the final layer history) ----
# ---- Stage 1: fetch the Vosk models, Persian + English (kept out of the final layer history) ----
FROM ${PYTHON_IMAGE} AS vosk-model
ARG VOSK_MODEL=vosk-model-small-fa-0.42
ARG VOSK_MODEL_URL=https://alphacephei.com/vosk/models/${VOSK_MODEL}.zip
# Optional: pin the archive hash for a reproducible, tamper-evident build.
ARG VOSK_MODEL_SHA256=
ARG VOSK_MODEL_EN=vosk-model-small-en-us-0.15
ARG VOSK_MODEL_EN_URL=https://alphacephei.com/vosk/models/${VOSK_MODEL_EN}.zip
ARG VOSK_MODEL_EN_SHA256=
# A zip placed in ./models/ (e.g. downloaded in a browser) is used instead of downloading.
COPY models/ /tmp/local-models/
RUN python - <<'PY'
import hashlib, os, shutil, sys, time, urllib.request, zipfile

def fetch(name, url, want, dest):
    want = want.strip().lower()
    local, archive = f"/tmp/local-models/{name}.zip", f"/tmp/{name}.zip"
    if os.path.exists(local):
        print(f"Using local {name}.zip from ./models/", flush=True)
        shutil.copy(local, archive)
    else:
        for attempt in range(1, 6):
            try:
                print(f"Downloading {url} (attempt {attempt}/5)", flush=True)
                urllib.request.urlretrieve(url, archive)
                break
            except OSError as exc:
                print(f"  failed: {exc}", flush=True)
                if attempt == 5:
                    sys.exit(
                        "Could not download the Vosk model. Download it in a browser from\n"
                        f"  {url}\n"
                        f"and save it as models/{name}.zip in the project folder, then build again."
                    )
                time.sleep(5 * attempt)
    got = hashlib.sha256(open(archive, "rb").read()).hexdigest()
    print(f"{name} sha256 {got}", flush=True)
    if want and got != want:
        sys.exit(f"Vosk model hash mismatch for {name}: expected {want}, got {got}")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall("/tmp/extract")
    shutil.move(f"/tmp/extract/{name}", dest)
    os.remove(archive)

e = os.environ
fetch(e["VOSK_MODEL"], e["VOSK_MODEL_URL"], e.get("VOSK_MODEL_SHA256", ""), "/opt/vosk-model")
fetch(e["VOSK_MODEL_EN"], e["VOSK_MODEL_EN_URL"], e.get("VOSK_MODEL_EN_SHA256", ""), "/opt/vosk-model-en")
PY

# ---- Stage 2: application ----
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
# e.g. http://mirror.example.ir/debian  (empty = Debian's default mirrors)
ARG APT_MIRROR=

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg converts Telegram OGG/Opus to 16 kHz mono WAV for Vosk.
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s#http://deb.debian.org/debian#${APT_MIRROR}#g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user with a fixed UID, so volume ownership is predictable.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first (cached until the lockfile changes). Every package and hash is pinned.
COPY requirements.lock .
RUN pip install --index-url "$PIP_INDEX_URL" --require-hashes --no-deps -r requirements.lock

# Model before code: changing code does not re-copy 50 MB.
COPY --from=vosk-model /opt/vosk-model /opt/vosk-model
COPY --from=vosk-model /opt/vosk-model-en /opt/vosk-model-en
COPY --chown=app:app . .

# Created in the image so a fresh named volume inherits the app user's ownership.
RUN mkdir -p /data/voice && chown -R app:app /data

USER app

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
