FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    DOWNLOAD_DIR=/tmp/dinlebi/indirilenler \
    CACHE_DIR=/tmp/dinlebi/cache/hls

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /tmp/dinlebi/indirilenler /tmp/dinlebi/cache/hls

# Render $PORT değişkenini kendisi atar
CMD ["sh", "-c", "python server.py --serve --no-browser --port ${PORT:-8080}"]
