FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Системные зависимости минимальны: tini для корректной обработки SIGTERM.
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

# storage и logs монтируются как volume — конфиги переживут пересборку.
VOLUME ["/app/storage", "/app/logs"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py"]
