# Bubble Pod Studio — production image (Hostinger VPS / Docker)
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BUBBLEPOD_HOST=0.0.0.0 \
    BUBBLEPOD_PORT=7878 \
    BUBBLEPOD_COOKIE_SECURE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist projects, members, settings outside the container
VOLUME ["/app/user_data"]

EXPOSE 7878

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${BUBBLEPOD_PORT:-7878}/api/health" || exit 1

CMD ["python", "run_studio.py"]
