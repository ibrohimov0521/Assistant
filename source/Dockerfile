FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg fonts-dejavu-core poppler-utils tesseract-ocr tesseract-ocr-rus tesseract-ocr-uzb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 assistant \
    && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --no-create-home assistant

WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 config ./config
RUN mkdir -p /data /var/lib/assistant-profile /tmp/assistant-media /host/proc/net /host/storage \
    && chown -R 10001:10001 /data /var/lib/assistant-profile /tmp/assistant-media

USER 10001:10001
CMD ["python", "-m", "app.cli", "demo"]

