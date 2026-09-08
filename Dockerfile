# syntax=docker/dockerfile:1

# --- stage 1: build the React frontend ---------------------------------
FROM node:20-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# --- stage 2: python runtime ----------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# ffmpeg for capture/clipping, tesseract (+ English) for overlay OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        tesseract-ocr-eng \
        libglib2.0-0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCOUTHUB_ARCHIVE_ROOT=/media \
    SCOUTHUB_DATA_DIR=/data \
    SCOUTHUB_FRONTEND_DIST=/app/frontend

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=frontend /build/dist /app/frontend

RUN mkdir -p /data /media
VOLUME ["/data", "/media"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
