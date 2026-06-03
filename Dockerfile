FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libfreetype6 \
        libjpeg62-turbo \
        libopenjp2-7 \
        libtiff6 \
        libwebp7 \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_bot_simple.py .

CMD ["python", "telegram_bot_simple.py"]
