FROM python:3.11-slim AS dependencies

LABEL org.opencontainers.image.source="https://github.com/UntYEE/paper-parallel-reader" \
      org.opencontainers.image.description="Local parallel PDF and Chinese translation reader" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt requirements-ocr.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

FROM dependencies AS runtime

COPY backend ./backend
COPY scripts ./scripts
COPY viewer ./viewer

ENV APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    PAPER_DATA_DIR=/data \
    ENABLE_OCR=false

RUN mkdir -p /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()"
CMD ["python", "-m", "backend.server"]

FROM runtime AS runtime-ocr

RUN python -m pip install -r requirements-ocr.txt
ENV ENABLE_OCR=true
